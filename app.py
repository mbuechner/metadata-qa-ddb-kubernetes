import asyncio
import logging
import os
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO
from kubernetes import client, config

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, cors_allowed_origins="*")

# Kubernetes API Configuration
config.load_incluster_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

# Log stream
log_stream_active = False
log_stream_thread = None
log_stream_lock = threading.Lock()

namespace = "ddbmetadata-qa"
deployment_name = "ddbmetadata-qa"

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_pod')
async def start_pod():
    try:
        if not v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items:
            scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
            scale.spec.replicas = 1
            apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
            socketio.emit('status_update', {'message': 'Pod is starting...', 'status': 'Starting'})

        while True:
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if pods:
                pod_status = pods[0].status.phase
                socketio.emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status})
                if pod_status == "Running":
                    break
            await asyncio.sleep(1)  # Asynchrones Schlafen

    except client.exceptions.ApiException as e:
        socketio.emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'})


@socketio.on('delete_pod')
async def delete_pod():
    try:
        if v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items:
            # Scale the deployment to 0 replicas
            scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
            scale.spec.replicas = 0
            apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
            socketio.emit('status_update', {'message': 'Pod is stopping...', 'status': 'Stopping'})

        # Confirm the pod has stopped
        while True:
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if pods:
                pod_status = pods[0].status.phase
                socketio.emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status})
            else:
                socketio.emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'})
                break
            await asyncio.sleep(1)  # Asynchrones Schlafen

    except client.exceptions.ApiException as e:
        socketio.emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'})


async def broadcast_logs():
    global log_stream_active
    try:
        while True:
            # Check if the pod exists
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if not pods:
                socketio.emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'})
                log_stream_active = False
                break

            pod_name = pods[0].metadata.name

            try:
                # Stream logs directly from the Kubernetes API
                log_stream = v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    follow=True,
                    _preload_content=False
                ).stream()

                for line in log_stream:
                    if not log_stream_active:
                        return
                    socketio.emit('log_update', {'message': line.decode('utf-8').strip()})
            except client.exceptions.ApiException as e:
                socketio.emit('status_update', {'message': f"{str(e)}", 'status': 'Error'})
                await asyncio.sleep(1)

            # Check if the pod is terminated
            pod_status = pods[0].status.phase
            if pod_status != "Running":
                socketio.emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status})
                log_stream_active = False
                break

    except client.exceptions.ApiException as e:
        socketio.emit('log_update', {'message': f"Error: {str(e)}"})
        log_stream_active = False


@socketio.on('start_log_streaming')
async def start_log_streaming():
    global log_stream_thread, log_stream_active

    with log_stream_lock:
        if log_stream_active:
            socketio.emit('status_update', {'message': 'Log streaming is already active.'})
            return
        log_stream_active = True
        log_stream_thread = threading.Thread(target=broadcast_logs)
        log_stream_thread.start()
        socketio.emit('status_update', {'message': 'Log streaming started.', 'status': 'Active'})

@socketio.on('stop_log_streaming')
async def stop_log_streaming():
    global log_stream_active
    with log_stream_lock:
        log_stream_active = False
        socketio.emit('status_update', {'message': 'Log streaming stopped.', 'status': 'Stopped'})

@socketio.on('get_status')
async def get_status():
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
    if pods:
        pod_status = pods[0].status.phase
        socketio.emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status})
    else:
        socketio.emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'})

@socketio.on('connect')
async def connect():
    logging.info(f'{request.sid} connected')
    await get_status()

@socketio.on('disconnect')
async def disconnect():
    logging.info(f'{request.sid} disconnected')

@socketio.on_error_default
async def default_error_handler(e):
    logging.error(f'SocketIO Error: {request.event["message"]} ({request.event["args"]})')
    logging.exception(e)

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000)
