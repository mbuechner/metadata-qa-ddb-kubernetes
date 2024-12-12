from flask import Flask, request, jsonify, Response, render_template
from flask_compress import Compress
from kubernetes import client, config

# Initialize Flask app
app = Flask(__name__)
Compress(app)  # Enable GZIP compression

# Kubernetes API Configuration
config.load_incluster_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

namespace = "ddbmetadata-qa"
deployment_name = "ddbmetadata-qa"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/is_running', methods=['GET'])
def is_running():
    try:
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={deployment_name}").items
        if not pods:
            return jsonify({"running": False, "message": "No pods are currently running."})

        pod_status = pods[0].status.phase
        if pod_status == "Running":
            return jsonify({"running": True, "message": "Pod is running."})
        else:
            return jsonify({"running": False, "message": f"Pod is in status {pod_status}."})
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/start_pod', methods=['POST'])
def start_pod():
    try:
        scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
        scale.spec.replicas = 1
        apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
        return jsonify({"message": "Pod started successfully."})
    except client.exceptions.ApiException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete_pod', methods=['POST'])
def delete_pod():
    try:
        scale = apps_v1.read_namespaced_deployment_scale(name=deployment_name, namespace=namespace)
        scale.spec.replicas = 0
        apps_v1.replace_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=scale)
        return jsonify({"message": "Pod stopped successfully."})
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
            log_lines = v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=32  # Limit logs to the last 32 lines
            )

            for line in log_lines.splitlines():
                yield f"{line}\n"
        except client.exceptions.ApiException as e:
            yield f"Error: {str(e)}"

    return Response(stream_logs(), mimetype='text/plain')

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
