from flask import Flask, request, jsonify, render_template, Response
from kubernetes import client, config
import os

app = Flask(__name__)

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

@app.route('/start_pod', methods=['POST'])
def start_pod():
    try:
        # Get the deployment and set replicas to 1
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = 1
        apps_v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return jsonify({"message": f"Deployment {deployment_name} set to 1 replica."}), 201
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete_pod', methods=['POST'])
def delete_pod():
    try:
        # Get the deployment and set replicas to 0
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        deployment.spec.replicas = 0
        apps_v1.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=deployment)
        return jsonify({"message": f"Deployment {deployment_name} scaled down to 0 replicas."}), 200
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_logs', methods=['GET'])
def get_logs():
    def stream_logs():
        try:
            # Find pods by label selector
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
    app.run(debug=False, host='0.0.0.0', port=5000)
