#!/bin/bash
set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/opt/presign-api"
SERVICE_USER="ubuntu"
ENVOY_USER="envoy"
PYTHON_VERSION="3.10"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (use sudo)"
    exit 1
fi

log_info "Starting Presign API installation..."

# Step 1: Update system packages
log_info "Updating system packages..."
apt-get update -qq

# Step 2: Install Python and dependencies
log_info "Installing Python ${PYTHON_VERSION} and dependencies..."
apt-get install -y \
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-venv \
    python3-pip \
    git \
    curl \
    wget \
    build-essential \
    software-properties-common

# Step 3: Install Envoy Proxy
if ! command -v envoy &> /dev/null; then
    log_info "Installing Envoy Proxy..."

    # Add Envoy's GPG key and repository (for Ubuntu/Debian)
    curl -sL 'https://deb.dl.getenvoy.io/public/gpg.8115BA8E629CC074.key' | gpg --dearmor -o /usr/share/keyrings/getenvoy-keyring.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/getenvoy-keyring.gpg] https://deb.dl.getenvoy.io/public/deb/ubuntu $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/getenvoy.list
    apt-get update -qq
    apt-get install -y getenvoy-envoy || {
        log_warn "Failed to install via getenvoy, trying alternative method..."
        apt-get install -y envoy
    }
else
    log_info "Envoy already installed: $(envoy --version | head -n1)"
fi

# Step 4: Create service user if it doesn't exist
if ! id "$SERVICE_USER" &>/dev/null; then
    log_info "Creating service user: $SERVICE_USER"
    useradd -r -s /bin/bash -d /home/$SERVICE_USER -m $SERVICE_USER
else
    log_info "User $SERVICE_USER already exists"
fi

# Step 5: Create envoy user if it doesn't exist
if ! id "$ENVOY_USER" &>/dev/null; then
    log_info "Creating envoy user: $ENVOY_USER"
    useradd -r -s /bin/false $ENVOY_USER
else
    log_info "User $ENVOY_USER already exists"
fi

# Step 6: Create installation directory
log_info "Creating installation directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/logs"
mkdir -p /etc/envoy
mkdir -p /var/log/envoy

# Step 7: Copy application files
log_info "Copying application files..."
cp -r "$PROJECT_ROOT/presign_api" "$INSTALL_DIR/"

# Copy .env file if it exists
if [ -f "$PROJECT_ROOT/presign_api/.env" ]; then
    log_info "Copying .env configuration..."
    cp "$PROJECT_ROOT/presign_api/.env" "$INSTALL_DIR/presign_api/"
    chmod 600 "$INSTALL_DIR/presign_api/.env"
elif [ -f "$PROJECT_ROOT/.env" ]; then
    log_info "Copying .env from project root..."
    cp "$PROJECT_ROOT/.env" "$INSTALL_DIR/presign_api/"
    chmod 600 "$INSTALL_DIR/presign_api/.env"
else
    log_warn "No .env file found. Please create $INSTALL_DIR/presign_api/.env manually"
    log_warn "See presign_api/.env.example for reference"
fi

# Copy requirements.txt or pyproject.toml
# Prefer presign_api/requirements.txt over root requirements.txt
if [ -f "$PROJECT_ROOT/presign_api/requirements.txt" ]; then
    log_info "Using presign_api/requirements.txt"
    cp "$PROJECT_ROOT/presign_api/requirements.txt" "$INSTALL_DIR/"
elif [ -f "$PROJECT_ROOT/requirements.txt" ]; then
    cp "$PROJECT_ROOT/requirements.txt" "$INSTALL_DIR/"
elif [ -f "$PROJECT_ROOT/pyproject.toml" ]; then
    cp "$PROJECT_ROOT/pyproject.toml" "$INSTALL_DIR/"
    [ -f "$PROJECT_ROOT/poetry.lock" ] && cp "$PROJECT_ROOT/poetry.lock" "$INSTALL_DIR/"
fi

# Step 8: Set up Python virtual environment
log_info "Setting up Python virtual environment..."
cd "$INSTALL_DIR"

# Remove old venv if it exists
[ -d ".venv" ] && rm -rf .venv

python${PYTHON_VERSION} -m venv .venv
source .venv/bin/activate

# Upgrade pip
.venv/bin/pip install --upgrade pip setuptools wheel

# Step 9: Install Python dependencies
log_info "Installing Python dependencies..."
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    .venv/bin/pip install -r requirements.txt
elif [ -f "$INSTALL_DIR/pyproject.toml" ]; then
    .venv/bin/pip install poetry
    .venv/bin/poetry install --no-dev
else
    log_warn "No requirements.txt or pyproject.toml found"
    log_info "Installing minimal dependencies..."
    .venv/bin/pip install fastapi uvicorn boto3 pydantic-settings python-dotenv
fi

# Step 10: Set permissions
log_info "Setting file permissions..."
chown -R $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/logs"

# Secure .env file
if [ -f "$INSTALL_DIR/presign_api/.env" ]; then
    chown $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR/presign_api/.env"
    chmod 600 "$INSTALL_DIR/presign_api/.env"
fi

# Set Envoy log directory permissions
chown -R $ENVOY_USER:$ENVOY_USER /var/log/envoy
chmod -R 755 /var/log/envoy

# Step 11: Copy Envoy configuration
log_info "Installing Envoy configuration..."
cp "$SCRIPT_DIR/envoy-presign-api.yaml" /etc/envoy/envoy-presigned-api.yaml
chmod 644 /etc/envoy/envoy-presigned-api.yaml

# Step 12: Install systemd service files
log_info "Installing systemd service files..."

# Install presign-api service
cp "$SCRIPT_DIR/presign-api.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/presign-api.service

# Install envoy service
cp "$SCRIPT_DIR/envoy-presigned-api.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/envoy-presigned-api.service

# Step 13: Reload systemd
log_info "Reloading systemd daemon..."
systemctl daemon-reload

# Step 14: Enable services
log_info "Enabling services..."
systemctl enable presign-api.service
systemctl enable envoy-presigned-api.service

# Step 15: Validate configuration
log_info "Validating configuration..."

# Check if .env file exists and has required variables
if [ ! -f "$INSTALL_DIR/presign_api/.env" ]; then
    log_error "Missing .env file at $INSTALL_DIR/presign_api/.env"
    log_error "Please create this file before starting the service"
    exit 1
fi

# Check for required environment variables
if ! grep -q "S3_BUCKET" "$INSTALL_DIR/presign_api/.env"; then
    log_warn "S3_BUCKET not found in .env file"
fi

# Validate Envoy config
log_info "Validating Envoy configuration..."
if sudo -u $ENVOY_USER envoy --mode validate -c /etc/envoy/envoy-presigned-api.yaml 2>&1 | grep -q "OK"; then
    log_info "Envoy configuration is valid"
else
    log_error "Envoy configuration validation failed"
    sudo -u $ENVOY_USER envoy --mode validate -c /etc/envoy/envoy-presigned-api.yaml
    exit 1
fi

# Step 16: Start services
log_info "Starting services..."

# Start presign-api first
systemctl start presign-api.service
sleep 3

# Check if presign-api started successfully
if systemctl is-active --quiet presign-api.service; then
    log_info "✓ Presign API service started successfully"
else
    log_error "✗ Presign API service failed to start"
    log_error "Check logs with: journalctl -u presign-api.service -n 50"
    exit 1
fi

# Start envoy proxy
systemctl start envoy-presigned-api.service
sleep 2

# Check if envoy started successfully
if systemctl is-active --quiet envoy-presigned-api.service; then
    log_info "✓ Envoy proxy service started successfully"
else
    log_error "✗ Envoy proxy service failed to start"
    log_error "Check logs with: journalctl -u envoy-presigned-api.service -n 50"
    exit 1
fi

# Step 17: Final verification
log_info "Performing health checks..."

# Wait a moment for services to fully start
sleep 3

# Check presign-api health (internal)
if curl -sf http://127.0.0.1:3060/health > /dev/null; then
    log_info "✓ Presign API health check passed (port 3060)"
else
    log_warn "✗ Presign API health check failed"
fi

# Check Envoy health (external)
if curl -sf http://127.0.0.1:8060/health > /dev/null; then
    log_info "✓ Envoy proxy health check passed (port 8060)"
else
    log_warn "✗ Envoy proxy health check failed"
fi

# Check Envoy admin interface
if curl -sf http://127.0.0.1:9060/stats > /dev/null; then
    log_info "✓ Envoy admin interface accessible (port 9060)"
else
    log_warn "✗ Envoy admin interface check failed"
fi

# Step 18: Print summary
echo ""
log_info "=========================================="
log_info "Installation completed successfully!"
log_info "=========================================="
echo ""
log_info "Service Status:"
log_info "  Presign API:  $(systemctl is-active presign-api.service)"
log_info "  Envoy Proxy:  $(systemctl is-active envoy-presigned-api.service)"
echo ""
log_info "Endpoints:"
log_info "  API (internal):     http://127.0.0.1:3060"
log_info "  Proxy (external):   http://0.0.0.0:8060"
log_info "  Envoy admin:        http://127.0.0.1:9060"
echo ""
log_info "Useful Commands:"
log_info "  Status:        systemctl status presign-api envoy-presigned-api"
log_info "  Restart:       systemctl restart presign-api envoy-presigned-api"
log_info "  Logs (API):    journalctl -u presign-api -f"
log_info "  Logs (Envoy):  journalctl -u envoy-presigned-api -f"
log_info "  Stop:          systemctl stop presign-api envoy-presigned-api"
echo ""
log_info "Configuration Files:"
log_info "  App:           $INSTALL_DIR/presign_api/"
log_info "  Env:           $INSTALL_DIR/presign_api/.env"
log_info "  Envoy:         /etc/envoy/envoy-presigned-api.yaml"
log_info "  Logs:          $INSTALL_DIR/logs/"
log_info "  Envoy Logs:    /var/log/envoy/"
echo ""

if [ -n "$(grep -L "S3_BUCKET" "$INSTALL_DIR/presign_api/.env" 2>/dev/null)" ]; then
    log_warn "Don't forget to configure your .env file with:"
    log_warn "  - S3_BUCKET (S3 bucket name)"
    log_warn "  - AWS credentials or IAM role"
    echo ""
fi

log_info "Installation complete! 🚀"
