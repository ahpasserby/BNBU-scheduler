import base64
import binascii
import hashlib
import re

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 50
RETENTION_SECONDS = 86400
CAPABILITIES = {"paper": "A4", "color": "grayscale", "sides": "one-sided", "copies": 1}
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
MESSAGES = {
    "processing": "正在处理打印任务。",
    "submitted": "已交给学校队列，请到打印机刷卡取件。",
    "unknown": "提交结果待确认，请先查看学校待释放任务，勿重复提交。",
    "auth_failed": "学校账号验证失败，请核对本人的学校密码。",
    "invalid_pdf": "无法读取这份 PDF，请重新导出后再试。",
    "encrypted_pdf": "暂不支持加密 PDF，请先导出一份未加密的文件。",
    "too_many_pages": "目前每份 PDF 最多支持 50 页。",
    "too_large": "PDF 不能超过 10 MB。",
    "offline": "打印设备暂未就绪，请稍后再试。",
    "busy": "打印设备正在处理其他任务，请稍后再试。",
    "rate_limited": "操作较频繁，请稍后再试。",
    "auth_rate_limited": "学校密码验证失败次数较多，请等待 15 分钟再试。",
    "active_job": "你还有一个任务正在处理，请先查看任务状态。",
    "conversion_failed": "文件转换失败，没有提交到学校队列。",
    "restarted": "设备在提交前重启，本次没有发送到学校队列。",
    "bad_input": "请求参数不正确，请刷新后重试。",
    "conflict": "同一任务编号对应的文件发生变化，请检查任务记录。",
}


class PrintError(Exception):
    def __init__(self, code, status=400, unaccepted=False):
        self.code = code
        self.status = status
        self.unaccepted = unaccepted
        super().__init__(MESSAGES.get(code, MESSAGES["bad_input"]))


def decode_pdf(value):
    if not isinstance(value, str):
        raise PrintError("invalid_pdf", 422)
    if len(value) > ((MAX_BYTES + 2) // 3) * 4:
        raise PrintError("too_large", 413)
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise PrintError("invalid_pdf", 422) from None
    if not content or not content.startswith(b"%PDF-"):
        raise PrintError("invalid_pdf", 422)
    if len(content) > MAX_BYTES:
        raise PrintError("too_large", 413)
    return content


def fingerprint(content, username):
    return hashlib.sha256(username.encode() + b"\0" + content).hexdigest()


def public_job(row):
    if row is None:
        return None
    state = row["state"]
    if state == "sending":
        state = "processing"
    code = row["code"]
    return {key: row[key] for key in (
        "id", "idempotency_key", "pages", "created_at", "updated_at"
    )} | {"state": state, "code": code, "message": MESSAGES.get(code, MESSAGES["unknown"])}
