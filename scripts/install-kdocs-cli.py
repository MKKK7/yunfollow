"""Install the official fixed Linux CLI with a pinned SHA256; no credentials involved."""
import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = '2.7.1'
SHA256 = 'cd1d5999a5f2dea8efc444f3b767ed86a2138a7c1a568853e0778f562e820c5b'
if platform.machine() not in ('x86_64', 'AMD64'):
    raise SystemExit('This minimal deployment pins the Linux amd64 CLI')
url = f'https://wpsai.wpscdn.cn/skillhub/pro/v{VERSION}/releases/kdocs-cli-{VERSION}-linux-amd64.tar.gz'
with urllib.request.urlopen(url, timeout=60) as response:
    raw = response.read(32 * 1024 * 1024 + 1)
if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != SHA256:
    raise SystemExit('Official KDocs CLI checksum mismatch')
with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
    member = next(m for m in archive.getmembers() if m.isfile() and m.name.split('/')[-1] == 'kdocs-cli')
    content = archive.extractfile(member).read()
target = Path('/usr/local/bin/kdocs-cli')
target.write_bytes(content)
target.chmod(0o755)
print('Official KDocs CLI installed with pinned SHA256 verification')
