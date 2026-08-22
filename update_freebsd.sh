#!/bin/sh

# WebZFS Update Script for FreeBSD
# This script updates an existing WebZFS installation at /opt/webzfs
# For initial installation, use install_freebsd.sh instead

set -e

INSTALL_DIR="/opt/webzfs"
VENV_DIR="${INSTALL_DIR}/.venv"
LOG_FILE="${INSTALL_DIR}/update_log.txt"
WHEELS_DIR="${INSTALL_DIR}/.wheels"

# GitHub raw URL base for pre-compiled wheels
WHEELS_REPO_BASE="https://github.com/webzfs/webzfs-wheels/raw/main/wheelhouse"

# Wheel packages to download (these require compilation without pre-built wheels)
# Versions must match the pins in requirements.txt and the list in
# install_freebsd.sh. The wheels cached from the initial install only cover
# the versions pinned at install time; whenever requirements.txt bumps a
# pinned version, the update must fetch the matching new wheels or pip
# silently falls back to compiling from source.
WHEEL_PACKAGES="cryptography-49.0.0 markupsafe-3.0.3 psutil-7.2.2 pydantic_core-2.46.4 bcrypt-5.0.0 cffi-2.1.0 pynacl-1.6.2"

# Determine the source directory (where this script is located)
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to detect FreeBSD version and determine wheel directory.
# Mirrors the same function in install_freebsd.sh.
detect_freebsd_version() {
    # Get FreeBSD version (e.g., "14.3-RELEASE" or "15.1-RELEASE")
    FREEBSD_VERSION=$(freebsd-version -u 2>/dev/null || uname -r)
    MAJOR_VERSION=$(echo "$FREEBSD_VERSION" | cut -d. -f1)
    MINOR_VERSION=$(echo "$FREEBSD_VERSION" | cut -d. -f2 | cut -d- -f1)

    echo "Detected FreeBSD version: $FREEBSD_VERSION (major: $MAJOR_VERSION, minor: $MINOR_VERSION)"

    # Map to wheel directory based on major and minor version
    case "${MAJOR_VERSION}.${MINOR_VERSION}" in
        14.3)
            WHEEL_SUBDIR="freebsd14-3"
            WHEEL_PLATFORM="freebsd_14_3_release_amd64"
            ;;
        14.4)
            WHEEL_SUBDIR="freebsd14-4"
            WHEEL_PLATFORM="freebsd_14_4_release_p1_amd64"
            ;;
        15.0)
            WHEEL_SUBDIR="freebsd15-0"
            WHEEL_PLATFORM="freebsd_15_0_release_amd64"
            ;;
        15.1)
            WHEEL_SUBDIR="freebsd15-1"
            WHEEL_PLATFORM="freebsd_15_1_release_p1_amd64"
            ;;
        15.*)
            # Newer 15.x minor releases: fall back to the latest 15.x wheels we build
            printf "${YELLOW}Warning: FreeBSD ${MAJOR_VERSION}.${MINOR_VERSION} wheels not published yet.${NC}\n"
            printf "${YELLOW}Falling back to FreeBSD 15.1 wheels (should be compatible).${NC}\n"
            WHEEL_SUBDIR="freebsd15-1"
            WHEEL_PLATFORM="freebsd_15_1_release_p1_amd64"
            ;;
        *)
            printf "${YELLOW}Warning: FreeBSD ${MAJOR_VERSION}.${MINOR_VERSION} is not directly supported.${NC}\n"
            printf "${YELLOW}Attempting to use FreeBSD 14.3 wheels (may not work).${NC}\n"
            WHEEL_SUBDIR="freebsd14-3"
            WHEEL_PLATFORM="freebsd_14_3_release_amd64"
            ;;
    esac

    printf "${GREEN}✓${NC} Will use wheels from: $WHEEL_SUBDIR\n"
}

# Function to download pre-compiled wheels for the versions pinned in
# requirements.txt. Wheels already cached from the initial install or a
# previous update are kept and skipped. On download failure the update
# continues and pip falls back to source compilation for that package.
download_wheels() {
    echo "Downloading pre-compiled wheels..."
    mkdir -p "$WHEELS_DIR"

    WHEELS_URL="${WHEELS_REPO_BASE}/${WHEEL_SUBDIR}"
    DOWNLOAD_FAILED=0

    for pkg_version in $WHEEL_PACKAGES; do
        # Extract package name (replace - with _ for wheel filename)
        pkg_name=$(echo "$pkg_version" | sed 's/-[0-9].*//')
        version=$(echo "$pkg_version" | sed 's/.*-//')
        wheel_pkg_name=$(echo "$pkg_name" | tr '-' '_')

        # Determine ABI tag based on package.
        # cryptography publishes a stable-ABI (abi3) wheel; from 45.x onward
        # its minimum interpreter tag is cp311.  All other packages ship
        # version-specific cp311 wheels.
        case "$pkg_name" in
            cryptography)
                ABI_TAG="cp311-abi3"
                ;;
            *)
                ABI_TAG="cp311-cp311"
                ;;
        esac

        wheel_filename="${wheel_pkg_name}-${version}-${ABI_TAG}-${WHEEL_PLATFORM}.whl"
        wheel_url="${WHEELS_URL}/${wheel_filename}"
        wheel_path="${WHEELS_DIR}/${wheel_filename}"

        if [ -f "$wheel_path" ]; then
            printf "  ${GREEN}✓${NC} ${pkg_name} wheel already cached\n"
        else
            printf "  Downloading ${pkg_name}..."
            if fetch -q -o "$wheel_path" "$wheel_url" 2>/dev/null; then
                printf " ${GREEN}✓${NC}\n"
            else
                printf " ${RED}FAILED${NC}\n"
                printf "${YELLOW}Warning: Could not download wheel for ${pkg_name}${NC}\n"
                printf "  URL: ${wheel_url}\n"
                DOWNLOAD_FAILED=1
            fi
        fi
    done

    if [ "$DOWNLOAD_FAILED" -eq 1 ]; then
        printf "${YELLOW}Some wheels failed to download. pip will attempt to compile those packages from source.${NC}\n"
    else
        printf "${GREEN}✓${NC} All wheels available\n"
    fi
}

echo "========================================"
echo "WebZFS Update Script for FreeBSD"
echo "========================================"
echo

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then 
    printf "${RED}Error: This script must be run as root${NC}\n"
    echo "Please run: sudo $0"
    exit 1
fi

# Verify installation exists
if [ ! -d "$INSTALL_DIR" ]; then
    printf "${RED}Error: WebZFS installation not found at $INSTALL_DIR${NC}\n"
    echo "Please run install_freebsd.sh for initial installation"
    exit 1
fi

# Verify virtual environment exists
if [ ! -d "$VENV_DIR" ]; then
    printf "${RED}Error: Virtual environment not found at $VENV_DIR${NC}\n"
    echo "Please run install_freebsd.sh for initial installation"
    exit 1
fi

# Verify essential files exist in source directory
ESSENTIAL_FILES=".env.example requirements.txt package.json"
for file in $ESSENTIAL_FILES; do
    if [ ! -f "${SOURCE_DIR}/${file}" ]; then
        printf "${RED}Error: Essential file '${file}' not found in ${SOURCE_DIR}${NC}\n"
        echo "Please run this script from the WebZFS source directory containing all application files."
        exit 1
    fi
done

# Verify rc.d script exists
if [ ! -f "/usr/local/etc/rc.d/webzfs" ]; then
    printf "${RED}Error: rc.d service script not found${NC}\n"
    echo "Please run install_freebsd.sh for initial installation"
    exit 1
fi

# Check if service is running
SERVICE_WAS_RUNNING=false
if service webzfs status >/dev/null 2>&1; then
    SERVICE_WAS_RUNNING=true
    echo "Stopping WebZFS service..."
    service webzfs stop
    printf "${GREEN}✓${NC} Service stopped\n"
fi

echo

# Copy application files to installation directory (preserving config)
echo "Updating application files from $SOURCE_DIR to $INSTALL_DIR..."

# Use tar instead of rsync (more portable on FreeBSD)
# Create a temporary exclude file for patterns
EXCLUDE_FILE=$(mktemp)
cat > "$EXCLUDE_FILE" << 'EOF'
.venv
node_modules
.git
*.log
__pycache__
*.pyc
.env
.config
.wheels
config/gunicorn.conf.py
EOF

# Create a backup tar of the source, excluding unwanted files
(cd "$SOURCE_DIR" && tar cf - --exclude-from="$EXCLUDE_FILE" .) | \
    (cd "$INSTALL_DIR" && tar xf -)

rm -f "$EXCLUDE_FILE"

printf "${GREEN}✓${NC} Application files updated\n"
echo

# Pre-create JSON data files that newer versions introduced.
#
# FileStorageService and SMARTMonitoringService create these on first
# import if missing, but several Gunicorn workers import at the same
# moment during a restart and can race each other writing the same new
# file. Creating them here, before the service is restarted, means the
# workers only ever read files that already exist. This mirrors the same
# block in install_freebsd.sh.
DATA_DIR="${INSTALL_DIR}/.config/webzfs"
mkdir -p "${DATA_DIR}/progress"
mkdir -p "${DATA_DIR}/logs"

# Storage service files
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

# SMART monitoring service files
if [ ! -f "${DATA_DIR}/smart_test_history.json" ]; then
    echo '{"history": []}' > "${DATA_DIR}/smart_test_history.json"
fi
if [ ! -f "${DATA_DIR}/smart_scheduled_tests.json" ]; then
    echo '{}' > "${DATA_DIR}/smart_scheduled_tests.json"
fi

# Health analysis service files
if [ ! -f "${DATA_DIR}/health_reports.json" ]; then
    echo '{"reports": []}' > "${DATA_DIR}/health_reports.json"
fi
if [ ! -f "${DATA_DIR}/health_schedules.json" ]; then
    echo '{"schedules": [], "next_id": 1}' > "${DATA_DIR}/health_schedules.json"
fi

# /root/.config/webzfs is deliberately not seeded. WebZFS runs as root
# here, but the rc.d wrapper, run.sh, and the scheduled task crontab
# entries all export HOME=/opt/webzfs, so the files above are the ones
# actually read.

printf "${GREEN}✓${NC} Data files verified\n"
echo

# Update CAPTION in .env from .env.example
ENV_FILE="${INSTALL_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    # Extract new CAPTION from .env.example
    NEW_CAPTION=$(grep -E '^CAPTION=' "${SOURCE_DIR}/.env.example" | head -1)
    if [ -n "$NEW_CAPTION" ]; then
        # Update CAPTION in existing .env file
        if grep -q '^CAPTION=' "$ENV_FILE"; then
            # FreeBSD sed requires -i '' for in-place editing
            sed -i '' "s|^CAPTION=.*|${NEW_CAPTION}|" "$ENV_FILE"
            printf "${GREEN}✓${NC} Updated CAPTION to: ${NEW_CAPTION}\n"
        else
            # CAPTION not found in .env, add it at the top
            printf '%s\n' "${NEW_CAPTION}" | cat - "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
            printf "${GREEN}✓${NC} Added CAPTION: ${NEW_CAPTION}\n"
        fi
    fi
fi

echo

# Update dependencies
echo "Updating Python and Node.js dependencies..."
echo "(This may take a few minutes...)"
echo

cd "$INSTALL_DIR"

# Set environment for building
export HOME="$INSTALL_DIR"

# Check for gmake
if command -v gmake >/dev/null 2>&1; then
    export MAKE=$(command -v gmake)
fi

echo "Upgrading pip in virtual environment..."
.venv/bin/python3 -m pip install --upgrade pip > update_log.txt 2>&1

# Detect FreeBSD version and download the pre-compiled wheels matching the
# versions pinned in the new requirements.txt. Wheels cached from the
# initial install only cover the versions pinned at install time, so an
# update that bumps pinned versions must fetch new wheels here.
detect_freebsd_version
download_wheels

# Adapt wheel platform tags for local system (handles FreeBSD patch levels)
FIND_LINKS_FLAG=""
if [ -d "$WHEELS_DIR" ]; then
    LOCAL_PLATFORM=$(.venv/bin/python3 -c "import sysconfig; print(sysconfig.get_platform().replace('.', '_').replace('-', '_'))")
    for whl in "$WHEELS_DIR"/*.whl; do
        if [ -f "$whl" ]; then
            base_whl=$(basename "$whl")
            # Check if this wheel needs a platform-adapted copy
            if echo "$base_whl" | grep -q "freebsd_" && ! echo "$base_whl" | grep -q "$LOCAL_PLATFORM"; then
                # Extract the wheel's platform tag
                whl_platform=$(echo "$base_whl" | sed 's/.*-\(freebsd_[^.]*\)\.whl/\1/')
                new_whl=$(echo "$whl" | sed "s/${whl_platform}/${LOCAL_PLATFORM}/")
                if [ ! -f "$new_whl" ]; then
                    cp "$whl" "$new_whl"
                fi
            fi
        fi
    done
    FIND_LINKS_FLAG="--find-links=$WHEELS_DIR"
    printf "${GREEN}✓${NC} Using wheels from $WHEELS_DIR\n"
fi

echo "Updating Python dependencies..."
if ! .venv/bin/pip install $FIND_LINKS_FLAG -r requirements.txt >> update_log.txt 2>&1; then
    printf "${RED}Error: Failed to update Python dependencies${NC}\n"
    echo "Check ${INSTALL_DIR}/update_log.txt for details"
    exit 1
fi

echo "Updating Node.js dependencies..."
npm install >> update_log.txt 2>&1

echo "Rebuilding static assets..."
npm run build:css >> update_log.txt 2>&1

echo
printf "${GREEN}✓${NC} Python dependencies updated\n"
printf "${GREEN}✓${NC} Node.js dependencies updated\n"
printf "${GREEN}✓${NC} Static assets rebuilt\n"
echo

# Restart service if it was running
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo "Restarting WebZFS service..."
    service webzfs start
    printf "${GREEN}✓${NC} Service restarted\n"
    echo
else
    echo "WebZFS service was not running before update."
    printf "Do you want to start WebZFS now? (y/n): "
    read -r REPLY
    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        service webzfs start
        printf "${GREEN}✓${NC} WebZFS service started\n"
    fi
fi

echo
echo "========================================"
printf "${GREEN}Update Complete!${NC}\n"
echo "========================================"
echo
echo "WebZFS has been updated at: $INSTALL_DIR"
echo
echo "To check the service status:"
echo "  sudo service webzfs status"
echo
echo "To view logs:"
echo "  tail -f $INSTALL_DIR/gunicorn.log"
echo
echo "To access the web interface:"
echo "  http://localhost:26619"
echo
