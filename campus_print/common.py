import base64
import binascii
import hashlib
import json
import re

MAX_BYTES = 50 * 1024 * 1024
# Base64 document plus bounded JSON metadata; shared by both HTTP endpoints.
MAX_REQUEST_BYTES = ((MAX_BYTES + 2) // 3) * 4 + 1024 * 1024
MAX_PAGES = 300
MAX_COPIES = 100
# Printed faces are bounded by document pages and requested copies.
MAX_IMPRESSIONS = MAX_PAGES * MAX_COPIES
BULK_CONFIRMATION_THRESHOLD = 200
RETENTION_SECONDS = 86400
COLORS = ("grayscale", "color")
SIDES = ("one-sided", "two-sided-long-edge", "two-sided-short-edge")
DEFAULT_OPTIONS = {"color": "grayscale", "sides": "one-sided", "copies": 1}
# Output features an agent must advertise before the cloud forwards them.
FEATURES = ("color", "duplex", "copies")
# Office documents and images are converted to PDF on the agent before preview.
CONVERT = "convert"
ZIP, OLE = b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
DOCUMENT_TYPES = {
    "doc": OLE, "docx": ZIP, "odt": ZIP, "rtf": b"{\\rtf",
    "ppt": OLE, "pptx": ZIP, "odp": ZIP,
    "xls": OLE, "xlsx": ZIP, "ods": ZIP,
    "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n",
}
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
MESSAGES = {
    "processing": "正在处理打印任务。",
    "submitted": "已交给学校队列，请到打印机刷卡取件。",
    "unknown": "提交结果待确认，请先查看学校待释放任务，勿重复提交。",
    "auth_failed": "学校账号验证失败，请核对本人的学校密码。",
    "invalid_pdf": "无法读取这份 PDF，请重新导出后再试。",
    "encrypted_pdf": "暂不支持加密 PDF，请先导出一份未加密的文件。",
    "too_many_pages": f"每份 PDF 最多支持 {MAX_PAGES} 页。",
    "too_large": f"文件不能超过 {MAX_BYTES // 1024**2} MB。",
    "offline": "打印设备暂未就绪，请稍后再试。",
    "busy": "打印设备正在处理其他任务，请稍后再试。",
    "rate_limited": "操作较频繁，请稍后再试。",
    "auth_rate_limited": "学校密码验证失败次数较多，请等待 15 分钟再试。",
    "active_job": "你还有一个任务正在处理，请先查看任务状态。",
    "conversion_failed": "文件转换失败，没有提交到学校队列。",
    "restarted": "设备在提交前重启，本次没有发送到学校队列。",
    "bad_input": "请求参数不正确，请刷新后重试。",
    "conflict": "同一任务编号对应的文件或打印设置发生变化，请检查任务记录。",
    "unsupported_option": "打印设备暂不支持所选设置，请调整后再试。",
    "unsupported_format": "暂不支持这种文件，请导出为 PDF 后再试。",
    "convert_failed": "这份文件无法转换，请导出为 PDF 后再试。",
    "bulk_confirmation_required": "请确认本次打印总量后再提交。",
    "too_many_impressions": f"单次最多打印 {MAX_IMPRESSIONS} 面，请减少份数后再试。",
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


def decode_document(value, name):
    """Validate an uploaded Office document or image by extension and file signature."""
    ext = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
    signature = DOCUMENT_TYPES.get(ext)
    if signature is None:
        raise PrintError("unsupported_format", 422)
    if not isinstance(value, str):
        raise PrintError("unsupported_format", 422)
    if len(value) > ((MAX_BYTES + 2) // 3) * 4:
        raise PrintError("too_large", 413)
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise PrintError("unsupported_format", 422) from None
    if len(content) > MAX_BYTES:
        raise PrintError("too_large", 413)
    if not content.startswith(signature):
        raise PrintError("unsupported_format", 422)
    return content, ext


def parse_options(value, features=FEATURES):
    """Validate output options; anything beyond the defaults needs a feature."""
    if value is None:
        return dict(DEFAULT_OPTIONS)
    if not isinstance(value, dict) or not set(value) <= set(DEFAULT_OPTIONS):
        raise PrintError("bad_input")
    options = DEFAULT_OPTIONS | value
    copies = options["copies"]
    if options["color"] not in COLORS or options["sides"] not in SIDES or type(copies) is not int or not 1 <= copies <= MAX_COPIES:
        raise PrintError("bad_input")
    needed = {"color"} if options["color"] != "grayscale" else set()
    needed |= {"duplex"} if options["sides"] != "one-sided" else set()
    needed |= {"copies"} if copies != 1 else set()
    if not needed <= set(features):
        raise PrintError("unsupported_option", 422)
    return options


def encode_options(options):
    return "" if options == DEFAULT_OPTIONS else json.dumps(options, sort_keys=True, separators=(",", ":"))


def decode_options(text):
    try:
        return parse_options(json.loads(text)) if text else dict(DEFAULT_OPTIONS)
    except (ValueError, PrintError):
        return dict(DEFAULT_OPTIONS)


def capabilities(features):
    features = set(features)
    return {
        "paper": "A4",
        "color": list(COLORS) if "color" in features else ["grayscale"],
        "sides": list(SIDES) if "duplex" in features else ["one-sided"],
        "copies": {"min": 1, "max": MAX_COPIES if "copies" in features else 1},
        "max_impressions": MAX_IMPRESSIONS,
        "bulk_confirmation_threshold": BULK_CONFIRMATION_THRESHOLD,
    }


def fingerprint(content, username, options=None):
    # Default options keep the original digest, so earlier intents still match.
    digest = hashlib.sha256(username.encode() + b"\0")
    digest.update(content)
    extra = encode_options(options or DEFAULT_OPTIONS)
    if extra:
        digest.update(b"\0" + extra.encode())
    return digest.hexdigest()


def confirm_volume(confirmation, content, pages, options):
    """Bind explicit bulk approval to this document and its exact output options."""
    if pages * options['copies'] <= BULK_CONFIRMATION_THRESHOLD:
        return
    expected = {'sha256': hashlib.sha256(content).hexdigest(), 'pages': pages, 'options': options}
    if confirmation != expected:
        raise PrintError('bulk_confirmation_required', 422, True)


def public_job(row):
    if row is None:
        return None
    state = row["state"]
    if state == "sending":
        state = "processing"
    code = row["code"]
    return {key: row[key] for key in (
        "id", "idempotency_key", "pages", "created_at", "updated_at"
    )} | {"state": state, "code": code, "message": MESSAGES.get(code, MESSAGES["unknown"]),
          "options": decode_options(row["options"] if "options" in row.keys() else "")}
