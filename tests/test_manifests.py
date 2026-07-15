from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resources(filename: str) -> dict[str, dict]:
    documents = yaml.safe_load_all((PROJECT_ROOT / "k8s" / filename).read_text(encoding="utf-8"))
    return {document["kind"]: document for document in documents}


class DeploymentManifestTests(unittest.TestCase):
    def test_container_disables_unused_gunicorn_control_socket(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('"--no-control-socket"', dockerfile)

    def test_openshift_example_uses_root_path_by_default(self) -> None:
        manifests = resources("openshift.yaml")
        prefix = manifests["ConfigMap"]["data"]["URL_PREFIX"]
        pod_spec = manifests["Deployment"]["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        self.assertEqual(prefix, "/")
        self.assertEqual(manifests["Route"]["spec"]["path"], prefix)
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(
            manifests["Route"]["spec"]["tls"],
            {"termination": "edge", "insecureEdgeTerminationPolicy": "Allow"},
        )

    def test_kubernetes_example_uses_root_path_by_default(self) -> None:
        deployment = resources("deployment.yaml")["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item.get("value") for item in container["env"]}

        self.assertEqual(environment["URL_PREFIX"], "/")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")


if __name__ == "__main__":
    unittest.main()
