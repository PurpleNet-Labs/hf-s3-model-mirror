# Quick Start - Deploy to EC2

You're already on an EC2 instance! Follow these steps to deploy the presign API.

## Step 1: Verify IAM Role (CRITICAL)

Check if the EC2 instance has the required IAM role attached:

```bash
# Check IAM role
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/

# Test S3 access
aws s3 ls s3://glycosense-models-v1/models/ --region us-east-1
```

**If you see "Access Denied" or no output**, the IAM role is NOT attached. You must:
1. Stop this instance
2. Attach the IAM role `PresignAPI-EC2-InstanceProfile` to it
3. Start the instance again

See `DEPLOYMENT.md` Step 1 for creating the IAM role.

---

## Step 2: Copy Code to EC2

From your **local machine**, copy the code to the EC2 instance:

```bash
# Get the EC2 public IP from AWS Console or:
aws ec2 describe-instances --filters "Name=tag:Name,Values=presign-api-server" --query 'Reservations[*].Instances[*].PublicIpAddress' --output text

# Copy the code (replace <EC2_PUBLIC_IP> and path to your key)
scp -i /path/to/your-key.pem -r presign_api ubuntu@<EC2_PUBLIC_IP>:/tmp/

# SSH to EC2
ssh -i /path/to/your-key.pem ubuntu@<EC2_PUBLIC_IP>

# Move to /opt
sudo mkdir -p /opt/presign-api
sudo chown ubuntu:ubuntu /opt/presign-api
mv /tmp/presign_api /opt/presign-api/

# Also copy deployment files
scp -i /path/to/your-key.pem DEPLOYMENT.md ubuntu@<EC2_PUBLIC_IP>:/opt/presign-api/
```

**OR** clone from Git (if you have a repository):

```bash
sudo mkdir -p /opt/presign-api
sudo chown ubuntu:ubuntu /opt/presign-api
cd /opt/presign-api
git clone https://github.com/your-org/hf-s3-model-mirror.git .
```

---

## Step 3: Run Deployment Script

On the EC2 instance, run the automated deployment script:

```bash
cd /opt/presign-api/presign_api
chmod +x deploy.sh
./deploy.sh
```

The script will:
- Install Python 3.11 and dependencies
- Create virtual environment
- Install Python packages
- Ask you for configuration values (S3 bucket, etc.)
- Install and start the systemd service

There is no JWT secret to configure: rhmp-services' Envoy authenticates requests and sets
`X-User-Id` (see `presign_api/README.md`, Authentication).

---

## Step 4: Manual Configuration (Alternative)

If you prefer manual setup instead of the script:

### 4.1 Install Dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip git

cd /opt/presign-api
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r presign_api/requirements.txt
```

### 4.2 Create .env File

```bash
cat > presign_api/.env <<'EOF'
# AWS Configuration (using IAM role - no keys needed)
AWS_REGION=us-east-1

# S3 Configuration
S3_BUCKET=glycosense-models-v1
S3_PREFIX_BASE=models/indicconformer
S3_LLM_PREFIX_BASE=models/llm

# Pre-signed URL TTL (15 minutes)
PRESIGN_TTL_SECONDS=900

EOF

chmod 600 presign_api/.env
```

### 4.3 Install Systemd Service

```bash
sudo cp presign_api/presign-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable presign-api
sudo systemctl start presign-api
```

---

## Step 5: Verify Deployment

### 5.1 Check Service Status

```bash
sudo systemctl status presign-api
```

Expected output: **active (running)**

### 5.2 View Logs

```bash
sudo journalctl -u presign-api -f
```

Press `Ctrl+C` to exit

### 5.3 Test API Locally

```bash
# Test without auth (if auth disabled)
curl http://localhost:8000/v1/models

# Test with JWT token
export JWT_TOKEN="your-actual-jwt-token"
curl -H "Authorization: Bearer $JWT_TOKEN" http://localhost:8000/v1/models/hi
```

Expected response:
```json
{
  "bucket": "glycosense-models-v1",
  "key": "models/indicconformer/hi/indicconformer_stt_hi_hybrid_rnnt_large.nemo",
  "url": "https://glycosense-models-v1.s3.amazonaws.com/...",
  "expires_at": "2026-02-02T12:00:00+00:00"
}
```

### 5.4 Test from External Client

Get your EC2 public IP:

```bash
curl -s http://169.254.169.254/latest/meta-data/public-ipv4
```

From your local machine:

```bash
export JWT_TOKEN="your-jwt-token"
export EC2_IP="<your-ec2-public-ip>"

curl -H "Authorization: Bearer $JWT_TOKEN" http://$EC2_IP:8000/v1/models/hi
```

---

## Step 6: Security Group Configuration

Make sure your EC2 security group allows inbound traffic on port 8000:

```bash
# From AWS CLI (on your local machine)
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxx \
  --protocol tcp \
  --port 8000 \
  --cidr 0.0.0.0/0  # For testing - restrict to your app servers in production
```

**For production**, replace `0.0.0.0/0` with your application servers' IP range.

---

## Step 7: Production Setup (Optional - HTTPS)

For production, set up Nginx with SSL:

```bash
# Install Nginx
sudo apt-get install -y nginx certbot python3-certbot-nginx

# Configure Nginx (replace api.yourdomain.com)
sudo tee /etc/nginx/sites-available/presign-api > /dev/null <<EOF
server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Enable site
sudo ln -s /etc/nginx/sites-available/presign-api /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Get SSL certificate (requires domain pointing to EC2 IP)
sudo certbot --nginx -d api.yourdomain.com

# Update service to bind to localhost only
sudo sed -i 's/--host 0.0.0.0/--host 127.0.0.1/' /etc/systemd/system/presign-api.service
sudo systemctl daemon-reload
sudo systemctl restart presign-api

# Allow HTTPS in security group
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxx \
  --protocol tcp \
  --port 443 \
  --cidr 0.0.0.0/0
```

---

## Troubleshooting

### Service won't start

```bash
# Check logs for errors
sudo journalctl -u presign-api -n 50 --no-pager

# Common issues:
# 1. Missing .env file
# 2. Wrong Python path
# 3. Missing dependencies
```

### "Access Denied" errors

```bash
# Verify IAM role
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/

# Test S3 access
aws s3 ls s3://glycosense-models-v1/models/ --region us-east-1

# If fails, IAM role is not attached or has insufficient permissions
```

### Every request returns 401 "Authentication required"

The request did not come through rhmp-services' Envoy, so it has no `X-User-Id`. The public
route is `https://<api-host>/downloader/v1/...` (edge → rhmp Envoy `:8041` → ext_authz →
`:8060`). The presign log says "did not come through rhmp Envoy ext_authz".

---

## Useful Commands

```bash
# View logs in real-time
sudo journalctl -u presign-api -f

# Restart service
sudo systemctl restart presign-api

# Stop service
sudo systemctl stop presign-api

# Check service status
sudo systemctl status presign-api

# Test API locally
curl http://localhost:8000/v1/models

# Get public IP
curl -s http://169.254.169.254/latest/meta-data/public-ipv4
```

---

## Next Steps

1. ✅ Service deployed and running
2. ⬜ Set up HTTPS with domain name
3. ⬜ Configure monitoring (CloudWatch)
4. ⬜ Set up auto-scaling (optional)
5. ⬜ Document API for mobile team
6. ⬜ Test with real JWT tokens from auth service
