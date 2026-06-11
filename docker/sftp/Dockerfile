FROM atmoz/sftp:debian

# Drop-in sshd config (verbose logging so uploads show up in
# `docker compose logs sftp` during the demo).
COPY sshd_config /etc/ssh/sshd_config.d/zz-custom.conf
