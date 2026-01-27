#!/bin/bash
# Deployment script for LiveKit Agent Worker on EC2
# Usage: ./deploy.sh [build|start|stop|restart|logs|status]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="livekit-agent"
CONTAINER_NAME="livekit-agent"
ENV_FILE=".env.local"
ECR_REGISTRY="241511115902.dkr.ecr.ap-south-1.amazonaws.com"
ECR_REPO="tata-chemicals/worker"
AWS_REGION="ap-south-1"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    
    if ! sudo docker info &> /dev/null; then
        log_error "Docker daemon is not running. Please start Docker."
        exit 1
    fi
}

check_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        log_error "Environment file $ENV_FILE not found!"
        log_info "Please create $ENV_FILE from env.example and fill in the values."
        exit 1
    fi
}

build_image() {
    log_info "Building Docker image: $IMAGE_NAME"
    check_env_file
    
    sudo docker build -f Dockerfile.agent -t "$IMAGE_NAME:latest" .
    
    if [ $? -eq 0 ]; then
        log_info "Image built successfully!"
    else
        log_error "Image build failed!"
        exit 1
    fi
}

stop_container() {
    log_info "Stopping container: $CONTAINER_NAME"
    
    if sudo docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        sudo docker stop "$CONTAINER_NAME" 2>/dev/null || true
        log_info "Container stopped."
    else
        log_warn "Container $CONTAINER_NAME does not exist."
    fi
}

remove_container() {
    log_info "Removing container: $CONTAINER_NAME"
    
    if sudo docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        sudo docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        log_info "Container removed."
    else
        log_warn "Container $CONTAINER_NAME does not exist."
    fi
}

start_container() {
    log_info "Starting container: $CONTAINER_NAME"
    check_env_file
    
    # Stop and remove existing container if it exists
    stop_container
    remove_container
    
    # Start new container
    sudo docker run -d \
        --restart unless-stopped \
        --name "$CONTAINER_NAME" \
        --env-file "$ENV_FILE" \
        --network host \
        "$IMAGE_NAME:latest"
    
    if [ $? -eq 0 ]; then
        log_info "Container started successfully!"
        log_info "View logs with: ./deploy.sh logs"
    else
        log_error "Failed to start container!"
        exit 1
    fi
}

restart_container() {
    log_info "Restarting container: $CONTAINER_NAME"
    stop_container
    start_container
}

show_logs() {
    log_info "Showing logs for: $CONTAINER_NAME"
    sudo docker logs -f "$CONTAINER_NAME"
}

show_status() {
    log_info "Container status:"
    sudo docker ps -a --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    
    if sudo docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_info "\nRecent logs (last 20 lines):"
        sudo docker logs --tail 20 "$CONTAINER_NAME"
    fi
}

push_to_ecr() {
    log_info "Pushing image to ECR: $ECR_REGISTRY/$ECR_REPO"
    
    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region "$AWS_REGION" | \
        sudo docker login --username AWS --password-stdin "$ECR_REGISTRY"
    
    # Tag image
    ECR_IMAGE="$ECR_REGISTRY/$ECR_REPO:latest"
    sudo docker tag "$IMAGE_NAME:latest" "$ECR_IMAGE"
    
    # Push image
    sudo docker push "$ECR_IMAGE"
    
    if [ $? -eq 0 ]; then
        log_info "Image pushed successfully to ECR!"
    else
        log_error "Failed to push image to ECR!"
        exit 1
    fi
}

pull_from_ecr() {
    log_info "Pulling image from ECR: $ECR_REGISTRY/$ECR_REPO"
    
    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region "$AWS_REGION" | \
        sudo docker login --username AWS --password-stdin "$ECR_REGISTRY"
    
    # Pull image
    ECR_IMAGE="$ECR_REGISTRY/$ECR_REPO:latest"
    sudo docker pull "$ECR_IMAGE"
    sudo docker tag "$ECR_IMAGE" "$IMAGE_NAME:latest"
    
    if [ $? -eq 0 ]; then
        log_info "Image pulled successfully from ECR!"
    else
        log_error "Failed to pull image from ECR!"
        exit 1
    fi
}

# Main script logic
check_docker

case "${1:-help}" in
    build)
        build_image
        ;;
    start)
        start_container
        ;;
    stop)
        stop_container
        ;;
    restart)
        restart_container
        ;;
    logs)
        show_logs
        ;;
    status)
        show_status
        ;;
    push)
        push_to_ecr
        ;;
    pull)
        pull_from_ecr
        ;;
    deploy)
        log_info "Full deployment: build -> start"
        build_image
        start_container
        ;;
    *)
        echo "Usage: $0 {build|start|stop|restart|logs|status|push|pull|deploy}"
        echo ""
        echo "Commands:"
        echo "  build   - Build the Docker image"
        echo "  start   - Start the container (stops/removes existing first)"
        echo "  stop    - Stop the container"
        echo "  restart - Restart the container"
        echo "  logs    - Show container logs (follow mode)"
        echo "  status  - Show container status and recent logs"
        echo "  push    - Push image to AWS ECR"
        echo "  pull    - Pull image from AWS ECR"
        echo "  deploy  - Build and start (full deployment)"
        exit 1
        ;;
esac
