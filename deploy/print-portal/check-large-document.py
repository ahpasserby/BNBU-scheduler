"""Exercise a 50 MiB / 300 page synthetic PDF locally; never submit a print job.

Run as the agent user in an isolated systemd unit with the production resource
limits. Only sandboxed pdfinfo and Ghostscript are called. No school credentials.
"""
import base64
import gc
import json
from pathlib import Path
import random
import subprocess
import tempfile
import time

from campus_print.agent import PrintAgent
from campus_print.common import MAX_BYTES, MAX_PAGES


def fixture(path):
    rng = random.Random(42)
    offsets = [0]
    with path.open('wb') as out:
        out.write(b'%PDF-1.4\n')
        def obj(number, body):
            offsets.append(out.tell())
            out.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
        obj(1, b'<< /Type /Catalog /Pages 2 0 R >>')
        kids = ' '.join(f'{3 + n * 3} 0 R' for n in range(MAX_PAGES))
        obj(2, f'<< /Type /Pages /Count {MAX_PAGES} /Kids [{kids}] >>'.encode())
        for n in range(MAX_PAGES):
            ident = 3 + n * 3
            obj(ident, f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im {ident+2} 0 R >> >> /Contents {ident+1} 0 R >>'.encode())
            stream = b'q 500 0 0 500 40 140 cm /Im Do Q\n'
            obj(ident+1, f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'endstream')
            pixels = rng.randbytes(240 * 240 * 3)
            obj(ident+2, f'<< /Type /XObject /Subtype /Image /Width 240 /Height 240 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length {len(pixels)} >>\nstream\n'.encode() + pixels + b'\nendstream')
        xref = out.tell()
        out.write(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
        for offset in offsets[1:]:
            out.write(f'{offset:010d} 00000 n \n'.encode())
        out.write(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
        assert out.tell() < MAX_BYTES
        out.write(b'%' + b' ' * (MAX_BYTES - out.tell() - 1))


def main():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        agent = PrintAgent(root / 'state', root / 'runtime', cleanup=False)
        assert agent.sandbox_ready, 'Sandbox unavailable'
        doc = root / 'fixture.pdf'
        fixture(doc)
        start = time.monotonic()
        inspected = agent.inspect({'pdf': base64.b64encode(doc.read_bytes()).decode()})
        assert inspected == {'pages': MAX_PAGES, 'bytes': MAX_BYTES}, inspected
        print(json.dumps({'inspection': inspected, 'seconds': round(time.monotonic()-start, 2)}), flush=True)
        gc.collect()
        for color in ('grayscale', 'color'):
            with tempfile.TemporaryDirectory(dir=agent.runtime_dir) as folder:
                folder = Path(folder)
                # Hard link avoids a redundant 50 MiB fixture copy in tmpfs.
                (folder / 'document.pdf').hardlink_to(doc)
                start = time.monotonic()
                try:
                    spool = agent.convert(folder, 'a' * 32, {'color': color, 'sides': 'two-sided-long-edge', 'copies': 100})
                except subprocess.CalledProcessError as exc:
                    # Diagnostics contain only the generated synthetic document.
                    print((exc.stdout + exc.stderr)[-3000:].decode('utf-8', 'replace'), flush=True)
                    raise
                with spool.open('rb') as stream:
                    setup = stream.read(1024 * 1024)
                assert b'<</NumCopies 100>> setpagedevice' in setup
                assert b'<</Duplex true /Tumble false>> setpagedevice' in setup
                print(json.dumps({'color': color, 'ps_bytes': spool.stat().st_size, 'seconds': round(time.monotonic()-start, 2)}), flush=True)
        assert not list(agent.runtime_dir.iterdir()), 'Temporary files remain'
        print('PASS: inspection, grayscale, color, duplex/copy setup, cleanup; no SMB submission', flush=True)


if __name__ == '__main__':
    main()
