from flask import Flask, request, jsonify, render_template, Response
from flask_compress import Compress
from kubernetes import client, config
import os

app = Flask(__name__)
Compress(app)  # Enable GZIP compression

# Load in-cluster configuration
config.load_incluster_config()

# Kubernetes API objects
v1 = client.CoreV1Api()  # For Pods
apps_v1 = client.AppsV1Api()  # For Deployments

deployment_name = "ddbmetadata-qa"  # Fixed deployment name
namespace = "ddbmetadata-qa"  # Fixed namespace

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/is_running', methods=['GET'])
def is_running():
    try:
        # Check the pod status
        pod_list = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
        if not pod_list:
            return jsonify({"running": False, "message": "No pods are currently running."}), 200

        pod_status = pod_list[0].status.phase
        if pod_status == "Running":
            return jsonify({"running": True, "message": f"Pod {pod_list[0].metadata.name} is running."}), 200
        else:
            return jsonify({"running": False, "message": f"Pod {pod_list[0].metadata.name} is in status {pod_status}."}), 200

    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/start_pod', methods=['POST'])
def start_pod():
    try:
        # Get the current scale and set replicas to 1
        scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
        scale.spec.replicas = 1
        apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
        return jsonify({"message": f"Deployment {deployment_name} scaled to 1 replica."}), 201
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete_pod', methods=['POST'])
def delete_pod():
    try:
        # Get the current scale and set replicas to 0
        scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
        scale.spec.replicas = 0
        apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
        return jsonify({"message": f"Deployment {deployment_name} scaled to 0 replicas."}), 200
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_logs', methods=['GET'])
def get_logs():
    def stream_logs():
        try:
            # Find the pods using the label selector
            pod_list = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if not pod_list:
                yield "No pods are currently running."
                return

            pod_name = pod_list[0].metadata.name
            log_lines = v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=100,  # Limit logs to the last 100 lines
                follow=True,
                _preload_content=False
            ).stream()

            for line in log_lines:
                yield line.decode('utf-8')
        except client.exceptions.ApiException as e:
            yield f"Error: {str(e)}"

    return Response(stream_logs(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
