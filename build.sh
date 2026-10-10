#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
cp src/install-base.sh install.sh
for item in 'src/vpsctl.py:/usr/local/bin/vpsctl:VPSCTL_SCRIPT' 'src/receive-deploy.sh:/usr/local/libexec/vps-kit/receive-deploy:RECEIVER_SCRIPT' 'src/extract-bundle.py:/usr/local/libexec/vps-kit/extract-bundle.py:EXTRACTOR_SCRIPT'; do
  IFS=: read -r src dst delimiter <<< "$item"
  printf 'cat > %s <<'"'"'%s'"'"'\n' "$dst" "$delimiter" >> install.sh
  cat "$src" >> install.sh
  printf '\n%s\n' "$delimiter" >> install.sh
done
cat src/install-tail.sh >> install.sh
chmod +x install.sh
bash -n install.sh
python3 -m py_compile src/vpsctl.py src/extract-bundle.py
printf 'Built install.sh (%s bytes)\n' "$(stat -c %s install.sh)"
