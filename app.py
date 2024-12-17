import time
import logging
import os
import threading
from flask import Flask, render_template, request, copy_current_request_context
from flask_socketio import SocketIO, emit
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
def start_pod():
    global log_stream_active, log_stream_thread
    try:
        if not v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items:
            scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
            scale.spec.replicas = 1
            apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
            emit('status_update', {'message': 'Pod is starting...', 'status': 'Starting'}, broadcast=True)

        last_pod_status = ''
        while True:
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if pods:
                pod_status = pods[0].status.phase
                if last_pod_status != pod_status:
                    emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status}, broadcast=True)
                    last_pod_status = pod_status
                with log_stream_lock:
                    if pod_status == "Running" and not log_stream_active:
                        @copy_current_request_context
                        def thread_function():
                            broadcast_logs(app.app_context())

                        log_stream_active = True
                        log_stream_thread = threading.Thread(target=thread_function)
                        log_stream_thread.start()
                        break
            time.sleep(1)

    except client.exceptions.ApiException as e:
        emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'}, broadcast=True)


@socketio.on('delete_pod')
def delete_pod():
    try:
        if v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items:
            # Scale the deployment to 0 replicas
            scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
            scale.spec.replicas = 0
            apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
            emit('status_update', {'message': 'Pod is stopping...', 'status': 'Stopping'}, broadcast=True)

        # Confirm the pod has stopped
        while True:
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if pods:
                pod_status = pods[0].status.phase
                emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status}, broadcast=True)
            else:
                emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'}, broadcast=True)
                break
            time.sleep(1)  # Asynchrones Schlafen

    except client.exceptions.ApiException as e:
        emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'}, broadcast=True)


def broadcast_logs(context):
    global log_stream_active
    with context:
        try:
            while True:
                # Check if the pod exists
                pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
                if not pods:
                    emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'}, broadcast=True)
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
                        emit('log_update', {'message': line.decode('utf-8').strip()}, broadcast=True)
                except client.exceptions.ApiException as e:
                    emit('status_update', {'message': f"{str(e)}", 'status': 'Error'}, broadcast=True)

                # Check if the pod is terminated
                pod_status = pods[0].status.phase
                if pod_status != "Running":
                    emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status}, broadcast=True)
                    log_stream_active = False
                    break

                time.sleep(1)

        except client.exceptions.ApiException as e:
            emit('log_update', {'message': f"Error: {str(e)}"}, broadcast=True)
            log_stream_active = False

@socketio.on('get_status')
def get_status():
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
    if pods:
        pod_status = pods[0].status.phase
        emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status})
    else:
        emit('status_update', {'message': 'Pod has stopped.', 'status': 'Stopped'})

@socketio.on('connect')
def connect():
    logging.info(f'{request.sid} connected')
    get_status()

@socketio.on('disconnect')
def disconnect():
    logging.info(f'{request.sid} disconnected')

@socketio.on_error_default
def default_error_handler(e):
    logging.error(f'SocketIO Error: {request.event["message"]} ({request.event["args"]})')
    logging.exception(e)
