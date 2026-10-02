#!/usr/bin/env bash
# One-time setup of a fresh server for the hosted demos.
#
# Run as root on Ubuntu 24.04 (amd64 or arm64), passing the public half of the
# key GitHub Actions deploys with. OVH's Ubuntu template logs you in as
# `ubuntu`, not root, so go through sudo:
#
#   ssh ubuntu@<server> 'sudo bash -s' -- "$(cat ~/.ssh/breakdown-demo-ci.pub)" < demo/server/bootstrap.sh
#
# On a host that installs your key for root instead, `ssh root@<server> 'bash -s' ...`
# works the same way: your key is read from whichever account invoked this.
#
# Safe to re-run. It does not create .env or start anything; README.md has the
# remaining steps.
set -euo pipefail

ci_pubkey=${1:?usage: bootstrap.sh "<CI deploy public key>"}
[ "$(id -u)" -eq 0 ] || { echo "run as root (via sudo)" >&2; exit 1; }
# The account you logged in as, whose authorized_keys holds your own key.
# Checked up front: the sshd step below turns passwords off, and copying an
# empty key list into deploy's would leave nobody able to log in.
admin=${SUDO_USER:-root}
admin_keys="$(getent passwd "$admin" | cut -d: -f6)/.ssh/authorized_keys"
test -s "$admin_keys" || { echo "$admin_keys is missing or empty" >&2; exit 1; }
repo=https://github.com/PolycultureResearch/breakdown.git
checkout=/srv/breakdown

export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git ufw unattended-upgrades

echo "== docker (official apt repo; Ubuntu's docker.io has no compose plugin)"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
  "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "== deploy user"
# In the docker group, which is root-equivalent on this host. That is the
# honest description of what a deploy key can do here, so guard the key.
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
# Your own key (the one the host installed for $admin) plus the CI key.
cat "$admin_keys" /home/deploy/.ssh/authorized_keys <(echo "$ci_pubkey") \
  | sort -u > /home/deploy/.ssh/authorized_keys.new
mv /home/deploy/.ssh/authorized_keys.new /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

echo "== sshd: keys only"
# sshd keeps the FIRST value it reads for each setting, and reads the drop-ins
# in lexical order. Cloud images ship 50-cloud-init.conf, which can say
# `PasswordAuthentication yes`; a 99- file would lose to it silently. Hence
# 00-, and the check of what sshd actually resolved.
rm -f /etc/ssh/sshd_config.d/99-breakdown.conf
cat > /etc/ssh/sshd_config.d/00-breakdown.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
effective=$(sshd -T)
for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin without-password"; do
  grep -qx "$want" <<<"$effective" || { echo "sshd did not resolve '$want'; see /etc/ssh/sshd_config.d" >&2; exit 1; }
done
systemctl reload ssh

echo "== firewall (ufw)"
# Docker-published ports bypass ufw, so this does not guard Caddy's ports. It
# does not need to: compose.yaml publishes only 80/443, which are open here
# anyway. What it closes is anything else the host might listen on. SSH is
# allowed before enabling, so this cannot cut off the session running it.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "== unattended security upgrades (no automatic reboot)"
# A reboot restarts the containers cold and discards the prewarmed fits, so
# kernel updates wait for a deliberate reboot followed by a redeploy.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "== swap (4 GB cushion for PyTensor's compile spikes)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== checkout"
install -d -o deploy -g deploy /srv
if [ ! -d "$checkout/.git" ]; then
  sudo -u deploy git clone --quiet "$repo" "$checkout"
fi

echo
echo "done. next: create $checkout/demo/server/.env (see README.md)"
