#!/bin/bash

# WebZFS Update Script for Linux
# This script updates an existing WebZFS installation at /opt/webzfs
# For initial installation, use install_linux.sh instead

set -e

INSTALL_DIR="/opt/webzfs"
VENV_DIR="${INSTALL_DIR}/.venv"
LOG_FILE="${INSTALL_DIR}/update_log.txt"
WEBZFS_USER="webzfs"

# Determine the source directory (where this script is located)
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo "WebZFS Update Script for Linux"
echo "========================================"
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Error: This script must be run as root${NC}"
    echo "Please run: sudo $0"
    exit 1
fi

# Verify installation exists
if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "${RED}Error: WebZFS installation not found at $INSTALL_DIR${NC}"
    echo "Please run install_linux.sh for initial installation"
    exit 1
fi

# Verify virtual environment exists
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${RED}Error: Virtual environment not found at $VENV_DIR${NC}"
    echo "Please run install_linux.sh for initial installation"
    exit 1
fi

# Verify essential files exist in source directory
ESSENTIAL_FILES=".env.example requirements.txt package.json"
for file in $ESSENTIAL_FILES; do
    if [ ! -f "${SOURCE_DIR}/${file}" ]; then
        echo -e "${RED}Error: Essential file '${file}' not found in ${SOURCE_DIR}${NC}"
        echo "Please run this script from the WebZFS source directory containing all application files."
        exit 1
    fi
done

# Verify webzfs user exists
if ! id "$WEBZFS_USER" &>/dev/null; then
    echo -e "${RED}Error: User '$WEBZFS_USER' does not exist${NC}"
    echo "Please run install_linux.sh for initial installation"
    exit 1
fi

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Node.js/npm preflight (GitHub issue #209).
# The update rebuilds CSS with npm install and npm run build:css as the
# webzfs service account. Verify a usable non-Snap Node.js 20+ toolchain
# exists before stopping the service or touching any files.
echo "Checking Node.js/npm prerequisites..."

if ! command_exists node; then
    echo -e "${RED}Error: Node.js is not installed${NC}"
    echo "Please install Node.js v20+ and try again"
    exit 1
fi

if ! command_exists npm; then
    echo -e "${RED}Error: npm is not installed${NC}"
    echo "Please install npm and try again"
    exit 1
fi

# Reject Snap-packaged Node.js/npm. The build steps run as the webzfs
# service account with HOME=/opt/webzfs. Snapd refuses to run snaps for
# users whose home directory is outside /home, which makes the Node Snap
# unusable for the WebZFS build. A system Node.js/npm must be installed
# instead. The existing Snap does not need to be removed.
NODE_REAL_PATH=$(readlink -f "$(command -v node)" 2>/dev/null || true)
NPM_REAL_PATH=$(readlink -f "$(command -v npm)" 2>/dev/null || true)

case "$NODE_REAL_PATH:$NPM_REAL_PATH" in
    /snap/*|*:/snap/*)
        echo -e "${RED}Error: Node.js/npm are installed as a Snap package${NC}"
        echo
        echo "The Node Snap cannot be used by the WebZFS updater because the"
        echo "build runs as the 'webzfs' service account with HOME=/opt/webzfs,"
        echo "and snapd rejects home directories outside of /home."
        echo
        echo "Please install a system (non-Snap) Node.js 20+ and npm, then"
        echo "rerun this updater. For example, on Debian/Ubuntu:"
        echo
        echo "  sudo apt update"
        echo "  sudo apt install nodejs npm"
        echo
        echo "The existing Node Snap does not need to be removed; the system"
        echo "packages can coexist with it. If both are installed, ensure the"
        echo "non-Snap node/npm come first in PATH."
        exit 1
        ;;
esac

# Enforce the Node.js 20+ requirement
NODE_VERSION=$(node --version 2>/dev/null | sed 's/^v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)

case "$NODE_MAJOR" in
    ''|*[!0-9]*)
        echo -e "${RED}Error: Unable to determine the Node.js version${NC}"
        echo "Please install Node.js v20 or newer and try again"
        exit 1
        ;;
esac

if [ "$NODE_MAJOR" -lt 20 ]; then
    echo -e "${RED}Error: Node.js 20+ is required (found ${NODE_VERSION})${NC}"
    echo "Please install Node.js v20 or newer and try again"
    exit 1
fi

echo -e "${GREEN}✓${NC} Node.js $(node --version) found"
echo -e "${GREEN}✓${NC} npm $(npm --version) found"


# Check if service is running
SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet webzfs 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    echo "Stopping WebZFS service..."
    systemctl stop webzfs
    echo -e "${GREEN}✓${NC} Service stopped"
fi

echo

# Copy application files to installation directory (preserving config)
echo "Updating application files from $SOURCE_DIR to $INSTALL_DIR..."
rsync -a --exclude='.venv' --exclude='node_modules' --exclude='.git' --exclude='*.log' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='.config' \
    --exclude='config/gunicorn.conf.py' \
    "${SOURCE_DIR}/" "$INSTALL_DIR/"

# Set ownership
chown -R "$WEBZFS_USER:$WEBZFS_USER" "$INSTALL_DIR"

echo -e "${GREEN}✓${NC} Application files updated"
echo

# Pre-create JSON data files that newer versions introduced.
#
# FileStorageService and SMARTMonitoringService create these on first
# import if missing, but several Gunicorn workers import at the same
# moment during a restart and can race each other writing the same new
# file. Creating them here, before the service is restarted, means the
# workers only ever read files that already exist. This mirrors the same
# block in install_linux.sh and is why an update must touch data files
# at all.
DATA_DIR="${INSTALL_DIR}/.config/webzfs"
mkdir -p "${DATA_DIR}/progress"
mkdir -p "${DATA_DIR}/logs"

if [ ! -f "${DATA_DIR}/replication_history.json" ]; then
    echo '{"executions": [], "next_id": 1}' > "${DATA_DIR}/replication_history.json"
fi
if [ ! -f "${DATA_DIR}/notification_log.json" ]; then
    echo '{"notifications": []}' > "${DATA_DIR}/notification_log.json"
fi
if [ ! -f "${DATA_DIR}/syncoid_jobs.json" ]; then
    echo '{"jobs": [], "next_id": 1}' > "${DATA_DIR}/syncoid_jobs.json"
fi
if [ ! -f "${DATA_DIR}/scrub_schedules.json" ]; then
    echo '{"schedules": [], "next_id": 1}' > "${DATA_DIR}/scrub_schedules.json"
fi
if [ ! -f "${DATA_DIR}/smart_test_history.json" ]; then
    echo '{"history": []}' > "${DATA_DIR}/smart_test_history.json"
fi
if [ ! -f "${DATA_DIR}/smart_scheduled_tests.json" ]; then
    echo '{}' > "${DATA_DIR}/smart_scheduled_tests.json"
fi
if [ ! -f "${DATA_DIR}/health_reports.json" ]; then
    echo '{"reports": []}' > "${DATA_DIR}/health_reports.json"
fi
if [ ! -f "${DATA_DIR}/health_schedules.json" ]; then
    echo '{"schedules": [], "next_id": 1}' > "${DATA_DIR}/health_schedules.json"
fi

chown -R "$WEBZFS_USER:$WEBZFS_USER" "$DATA_DIR"
echo -e "${GREEN}✓${NC} Data files verified"
echo

# Refresh sudo permissions so new privileged commands (for example grep and
# dmesg used by the support bundle log collectors) are whitelisted on existing
# installations. Writing the file on every update keeps it in sync with the
# installer.
SUDOERS_FILE="/etc/sudoers.d/webzfs"
echo "Refreshing sudo permissions..."

cat > "$SUDOERS_FILE" << 'SUDO_EOF'
# WebZFS sudo permissions
# Allow webzfs user to execute ZFS and SMART commands

# ZFS commands (multiple paths for different distributions)
webzfs ALL=(ALL) NOPASSWD: /usr/sbin/zpool, /usr/sbin/zfs, /usr/sbin/zdb -l *, /usr/bin/zpool, /usr/bin/zfs, /usr/bin/zdb -l *, /sbin/zpool, /sbin/zfs, /sbin/zdb -l *

# SMART monitoring (multiple paths for different distributions)
webzfs ALL=(ALL) NOPASSWD: /usr/sbin/smartctl, /usr/bin/smartctl, /sbin/smartctl

# Disk utilities
webzfs ALL=(ALL) NOPASSWD: /usr/bin/lsblk, /usr/bin/blkid

# Open file / lock inspection (pool export busy investigation)
webzfs ALL=(ALL) NOPASSWD: /usr/bin/lsof, /usr/bin/lslocks, /bin/lsof, /bin/lslocks

# Sanoid/Syncoid (optional)
webzfs ALL=(ALL) NOPASSWD: /usr/sbin/sanoid, /usr/sbin/syncoid, /usr/bin/sanoid, /usr/bin/syncoid, /usr/local/sbin/sanoid, /usr/local/sbin/syncoid

# Service management (systemctl for system services page)
webzfs ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /bin/systemctl

# Crontab editing
webzfs ALL=(ALL) NOPASSWD: /usr/bin/crontab

# Scheduled syncoid job timers.
# Unit files are created and edited with "sudo tee" (covered by the
# general tee entry below) and enabled/disabled/reloaded with
# "sudo systemctl" (covered by the systemctl entry above). The explicit
# tee entries here document that intent and keep timer management
# working even if the general tee entry is ever narrowed. rm is
# restricted to WebZFS-owned unit files only.
webzfs ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/webzfs-syncoid-job-*, /bin/tee /etc/systemd/system/webzfs-syncoid-job-*
webzfs ALL=(ALL) NOPASSWD: /usr/bin/rm -f /etc/systemd/system/webzfs-syncoid-job-*, /bin/rm -f /etc/systemd/system/webzfs-syncoid-job-*

# Unified Scheduling Hub timers. All scheduled task types (scrub, SMART
# self-test, health check, and replication) use the webzfs-task-* unit
# naming scheme managed by services/job_scheduler.py.
webzfs ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/webzfs-task-*, /bin/tee /etc/systemd/system/webzfs-task-*
webzfs ALL=(ALL) NOPASSWD: /usr/bin/rm -f /etc/systemd/system/webzfs-task-*, /bin/rm -f /etc/systemd/system/webzfs-task-*

# File editing (for config files like smartd.conf, sanoid.conf)
webzfs ALL=(ALL) NOPASSWD: /usr/bin/cat, /usr/bin/tee, /usr/bin/mkdir

# Read system journal and plain-text syslog files for the
# Observability -> System Log page. journalctl needs sudo (or
# systemd-journal group) on most distros. tail covers Debian/Ubuntu
# (/var/log/syslog) and old RHEL (/var/log/messages).
webzfs ALL=(ALL) NOPASSWD: /usr/bin/journalctl, /bin/journalctl, /usr/bin/tail, /bin/tail

# Support bundle log collection. Reading /var/log/messages and
# /var/log/syslog (typically mode 640 root:adm) and the kernel ring
# buffer requires elevated privileges for the unprivileged webzfs user.
webzfs ALL=(ALL) NOPASSWD: /usr/bin/grep, /bin/grep, /usr/bin/dmesg, /bin/dmesg
SUDO_EOF

chmod 0440 "$SUDOERS_FILE"
echo -e "${GREEN}✓${NC} Sudo permissions refreshed"
echo


# Update CAPTION in .env from .env.example
ENV_FILE="${INSTALL_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    # Extract new CAPTION from .env.example
    NEW_CAPTION=$(grep -E '^CAPTION=' "${SOURCE_DIR}/.env.example" | head -1)
    if [ -n "$NEW_CAPTION" ]; then
        # Update CAPTION in existing .env file
        if grep -q '^CAPTION=' "$ENV_FILE"; then
            sed -i "s|^CAPTION=.*|${NEW_CAPTION}|" "$ENV_FILE"
            echo -e "${GREEN}✓${NC} Updated CAPTION to: ${NEW_CAPTION}"
        else
            # CAPTION not found in .env, add it at the top
            echo "${NEW_CAPTION}" | cat - "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
            echo -e "${GREEN}✓${NC} Added CAPTION: ${NEW_CAPTION}"
        fi
        chown "$WEBZFS_USER:$WEBZFS_USER" "$ENV_FILE"
    fi
fi

echo

# Create a temporary update script that runs as the webzfs user
TEMP_UPDATE_SCRIPT="${INSTALL_DIR}/_update_deps.sh"
echo "Updating Python and Node.js dependencies as $WEBZFS_USER..."
echo "(This may take a few minutes...)"
echo

# Create the update script
cat > "$TEMP_UPDATE_SCRIPT" << UPDATE_EOF
#!/bin/bash
set -e

# Set HOME to the webzfs user's home directory for pip cache
export HOME="/opt/webzfs"

cd /opt/webzfs

echo "Upgrading pip in virtual environment..."
.venv/bin/python3 -m pip install --upgrade pip > update_log.txt 2>&1

echo "Updating Python dependencies..."
.venv/bin/pip install -r requirements.txt >> update_log.txt 2>&1

echo "Updating Node.js dependencies..."
npm install >> update_log.txt 2>&1

echo "Rebuilding static assets..."
npm run build:css >> update_log.txt 2>&1

echo "Dependencies updated successfully!"
UPDATE_EOF

chmod +x "$TEMP_UPDATE_SCRIPT"
chown "$WEBZFS_USER:$WEBZFS_USER" "$TEMP_UPDATE_SCRIPT"

# Run the update script as the webzfs user
if ! su -s /bin/bash "$WEBZFS_USER" -c "bash $TEMP_UPDATE_SCRIPT"; then
    echo -e "${RED}Error: Update failed${NC}"
    echo "Check $LOG_FILE for details"
    rm -f "$TEMP_UPDATE_SCRIPT"
    exit 1
fi

# Clean up the temporary script
rm -f "$TEMP_UPDATE_SCRIPT"

echo
echo -e "${GREEN}✓${NC} Python dependencies updated"
echo -e "${GREEN}✓${NC} Node.js dependencies updated"
echo -e "${GREEN}✓${NC} Static assets rebuilt"
echo

# Restart service if it was running
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo "Restarting WebZFS service..."
    systemctl daemon-reload
    systemctl start webzfs
    echo -e "${GREEN}✓${NC} Service restarted"
    echo
else
    echo "WebZFS service was not running before update."
    printf "Do you want to start WebZFS now? (y/n): "
    read -r REPLY
    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        systemctl daemon-reload
        systemctl start webzfs
        echo -e "${GREEN}✓${NC} WebZFS service started"
    fi
fi

echo
echo "========================================"
echo -e "${GREEN}Update Complete!${NC}"
echo "========================================"
echo
echo "WebZFS has been updated at: $INSTALL_DIR"
echo
echo "To check the service status:"
echo "  sudo systemctl status webzfs"
echo
echo "To view logs:"
echo "  sudo journalctl -u webzfs -f"
echo
echo "To access the web interface:"
echo "  http://localhost:26619"
echo
