# EC2 Deployment Guide

This guide explains how to deploy the LiveKit Agent Worker on an EC2 instance.

## Prerequisites

1. **EC2 Instance** with:
   - Ubuntu 20.04+ or Amazon Linux 2
   - Docker installed
   - AWS CLI configured (for ECR access)
   - Sufficient resources (recommended: 2+ vCPU, 4GB+ RAM)

2. **Environment Variables**:
   - Create `.env.local` file from `env.example`
   - Fill in all required values (LiveKit credentials, API keys, etc.)

## Quick Start

### Option 1: Using the Deployment Script (Recommended)

1. **Make the script executable**:
   ```bash
   chmod +x deploy.sh
   ```

2. **Build and start the agent**:
   ```bash
   ./deploy.sh deploy
   ```

3. **View logs**:
   ```bash
   ./deploy.sh logs
   ```

4. **Check status**:
   ```bash
   ./deploy.sh status
   ```

### Option 2: Manual Docker Commands

1. **Build the image**:
   ```bash
   sudo docker build -f Dockerfile.agent -t livekit-agent .
   ```

2. **Run the container**:
   ```bash
   sudo docker run -d \
       --restart unless-stopped \
       --name livekit-agent \
       --env-file .env.local \
       --network host \
       livekit-agent:latest
   ```

3. **View logs**:
   ```bash
   sudo docker logs -f livekit-agent
   ```

### Option 3: Using Systemd Service

1. **Copy the service file**:
   ```bash
   sudo cp livekit-agent.service /etc/systemd/system/
   ```

2. **Update the service file** with your actual path:
   ```bash
   sudo nano /etc/systemd/system/livekit-agent.service
   # Update WorkingDirectory to your actual path
   ```

3. **Reload systemd and enable**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable livekit-agent.service
   sudo systemctl start livekit-agent.service
   ```

4. **Check status**:
   ```bash
   sudo systemctl status livekit-agent.service
   ```

## Deployment Script Commands

The `deploy.sh` script provides the following commands:

| Command | Description |
|---------|-------------|
| `build` | Build the Docker image |
| `start` | Start the container (stops/removes existing first) |
| `stop` | Stop the container |
| `restart` | Restart the container |
| `logs` | Show container logs (follow mode) |
| `status` | Show container status and recent logs |
| `push` | Push image to AWS ECR |
| `pull` | Pull image from AWS ECR |
| `deploy` | Build and start (full deployment) |

## ECR Deployment (Production)

If you're using AWS ECR to store your Docker images:

1. **Build and push to ECR**:
   ```bash
   ./deploy.sh build
   ./deploy.sh push
   ```

2. **On EC2, pull and run**:
   ```bash
   ./deploy.sh pull
   ./deploy.sh start
   ```

## Environment Variables

Ensure `.env.local` contains:

- **LiveKit**:
  - `LIVEKIT_URL` - Your LiveKit server URL (e.g., `wss://your-server.livekit.cloud`)
  - `LIVEKIT_API_KEY` - LiveKit API key
  - `LIVEKIT_API_SECRET` - LiveKit API secret
  - `SIP_OUTBOUND_TRUNK_ID` - SIP trunk ID (or it will be auto-created)

- **Database**:
  - `DATABASE_URL` - PostgreSQL connection string

- **AI Services**:
  - `OPENAI_API_KEY` - OpenAI API key
  - `CARTESIA_API_KEY` - Cartesia API key (for TTS)
  - `SARVAM_API_KEY` - Sarvam API key (for STT)

- **SIP Trunk** (if auto-creating):
  - `SIP_TRUNK_ADDRESS` - SIP provider address
  - `SIP_TRUNK_AUTH_USERNAME` - SIP authentication username
  - `SIP_TRUNK_AUTH_PASSWORD` - SIP authentication password

## Troubleshooting

### Container won't start

1. **Check logs**:
   ```bash
   ./deploy.sh logs
   ```

2. **Verify environment file**:
   ```bash
   cat .env.local
   ```

3. **Check Docker**:
   ```bash
   sudo docker ps -a
   sudo docker logs livekit-agent
   ```

### Agent not connecting to LiveKit

1. **Verify LiveKit credentials** in `.env.local`
2. **Check network connectivity** to LiveKit server
3. **Verify LiveKit server is running** and accessible

### Container keeps restarting

1. **Check logs for errors**:
   ```bash
   sudo docker logs livekit-agent
   ```

2. **Verify all required environment variables are set**
3. **Check system resources** (CPU, memory)

## Updating the Agent

1. **Pull latest code**:
   ```bash
   git pull
   ```

2. **Rebuild and restart**:
   ```bash
   ./deploy.sh deploy
   ```

Or if using ECR:
```bash
./deploy.sh pull
./deploy.sh restart
```

## Monitoring

- **View real-time logs**: `./deploy.sh logs`
- **Check container status**: `./deploy.sh status`
- **Docker stats**: `sudo docker stats livekit-agent`

## Notes

- The container uses `--network host` to communicate with LiveKit server on the same host
- The container has `--restart unless-stopped` to automatically restart on failure
- Environment variables are loaded from `.env.local` file
- The agent runs `main.py start` which starts the LiveKit worker and waits for jobs
