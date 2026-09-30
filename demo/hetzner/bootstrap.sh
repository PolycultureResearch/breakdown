#!/usr/bin/env bash
# One-time setup of a fresh Hetzner server for the hosted demos.
#
# Run as root on Ubuntu 24.04 (arm64 or amd64), passing the public half of the
# key GitHub Actions deploys with:
#
#   ssh root@<server> 'bash -s' -- "$(cat ~/.ssh/breakdown-demo-ci.pub)" < demo/hetzner/bootstrap.sh
#
# Safe to re-run. It does not create .env or start anything; README.md has the
# remaining steps.
set -euo pipefail

ci_pubkey=${1:?usage: bootstrap.sh "<CI deploy public key>"}
repo=https://github.com/PolycultureResearch/breakdown.git
checkout=/srv/breakdown

export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git unattended-upgrades

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
# Your own key (the one Hetzner installed for root) plus the CI key.
cat /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys <(echo "$ci_pubkey") \
  | sort -u > /home/deploy/.ssh/authorized_keys.new
mv /home/deploy/.ssh/authorized_keys.new /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

echo "== sshd: keys only"
cat > /etc/ssh/sshd_config.d/99-breakdown.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl reload ssh

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
echo "done. next: create $checkout/demo/hetzner/.env (see README.md)"
