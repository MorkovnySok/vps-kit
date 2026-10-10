
chmod 755 /usr/local/bin/vpsctl /usr/local/libexec/vps-kit/receive-deploy /usr/local/libexec/vps-kit/extract-bundle.py
log 'Done. No existing application containers, Docker volumes, .env files or GitHub runners were changed.'
log 'Next: sudo vpsctl project add app --repo OWNER/REPO --port 8081 --compose compose.yaml'
log 'Then: sudo gh auth login --web && sudo vpsctl github configure app'
log 'Security: only 80/443 and your SSH port should be public; configure the host/cloud firewall yourself.'
log 'Warning: deploy user has Docker access (effectively root-level). Protect GitHub deployment secrets.'
