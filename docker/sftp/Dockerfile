FROM linuxserver/openssh-server:latest

# Drop-in sshd config (verbose logging so uploads show up in
# `docker compose logs sftp` during the demo). linuxserver/openssh-server
# runs sshd against /config/sshd/sshd_config, which it generates at startup
# and wires up to Include /config/sshd/sshd_config.d/*.conf if that directory
# exists - so the drop-in goes there, not /etc/ssh/sshd_config.d/.
COPY sshd_config /config/sshd/sshd_config.d/zz-custom.conf
