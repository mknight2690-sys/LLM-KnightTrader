"""Fix UTF-16 encoded Python files to UTF-8."""
import os
import glob

fixed = []
for f in glob.glob('**/*.py', recursive=True):
    with open(f, 'rb') as fh:
        raw = fh.read()
    if raw.startswith(b'\xff\xfe'):
        content = raw[2:].decode('utf-16-le')
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write(content)
        fixed.append(f)
    elif raw.startswith(b'\xfe\xff'):
        content = raw[2:].decode('utf-16-be')
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write(content)
        fixed.append(f)
    elif raw[1:2] == b'\x00' and not raw.startswith(b'\x00'):
        # UTF-16-LE without BOM
        try:
            content = raw.decode('utf-16-le')
            with open(f, 'w', encoding='utf-8') as fh:
                fh.write(content)
            fixed.append(f + ' (no-BOM)')
        except UnicodeDecodeError:
            pass

print('Fixed:', fixed)
