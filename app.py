from flask import Flask, request, jsonify, render_template, Response
from kubernetes import client, config
import threading
import time

app = Flask(__name__)

# Load Kubernetes configuration
config.load_kube_config()

v1 = client.CoreV1Api()

deployment_name = "ddbmetadata-qa"  # Fixed deployment name
namespace = "ddbmetadata-qa"  # Default namespace

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_pod', methods=['POST'])
def start_pod():
    try:
        deployment = v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = 1  # Ensure only one replica is running
        v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return jsonify({"message": f"Deployment {deployment_name} set to 1 replica."}), 201
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete_pod', methods=['POST'])
def delete_pod():
    try:
        deployment = v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = 0  # Scale down to 0 replicas
        v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return jsonify({"message": f"Deployment {deployment_name} scaled down to 0 replicas."}), 200
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_logs', methods=['GET'])
def get_logs():
    def stream_logs():
        try:
            pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
            if not pods:
                yield "No pods are currently running."
                return
            pod_name = pods[0].metadata.name
            for line in v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, follow=True, _preload_content=False).stream():
                yield line.decode('utf-8')
        except client.exceptions.ApiException as e:
            yield f"Error: {str(e)}"

    return Response(stream_logs(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(debug=True)
