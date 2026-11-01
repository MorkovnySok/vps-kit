
chmod 755 /usr/local/bin/vpsctl /usr/local/libexec/vps-kit/receive-deploy /usr/local/libexec/vps-kit/extract-bundle.py
chmod 644 /usr/local/libexec/vps-kit/vpskit_vpn.py
log 'Done. No existing application containers, Docker volumes, .env files or GitHub runners were changed.'
log 'Next: sudo vpsctl — главное меню; sudo vpsctl project add — мастер добавления проекта'
log 'Help: vpsctl --help; vpsctl help project add'
log 'Personal VPN: sudo vpsctl vpn setup — Headscale + HTTPS + own DERP for phone/PC'
log 'Then: sudo gh auth login --web && sudo vpsctl github configure app'
log 'Firewall: TCP 80/443 and your SSH port; personal VPN additionally needs UDP 3478. Configure host/cloud rules yourself.'
log 'Warning: deploy user has Docker access (effectively root-level). Protect GitHub deployment secrets.'
