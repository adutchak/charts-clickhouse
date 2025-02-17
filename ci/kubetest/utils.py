import logging
import subprocess
import tempfile
import time

import pytest
import yaml

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger()

YamlString = str

NAMESPACE = "posthog"

VALUES_DISABLE_EVERYTHING: YamlString = """
cloud: "local"
web:
    enabled: false
migrate:
    enabled: false
events:
    enabled: false
worker:
    enabled: false
plugins:
    enabled: false
postgresql:
    enabled: false
redis:
    enabled: false
kafka:
    enabled: false
ingress:
    enabled: false
pgbouncer:
    enabled: false
clickhouse:
    enabled: false
zookeeper:
    enabled: false
"""


def merge_yaml(*yamls):
    result = {}
    for value in yamls:
        result.update(yaml.safe_load(value))
    return yaml.dump(result)


def cleanup_k8s(namespaces=["default", NAMESPACE]):
    log.debug("🔄 Making sure the k8s cluster is empty...")

    exec_subprocess("kubectl delete clusterrolebinding clickhouse-operator-posthog --ignore-not-found")
    exec_subprocess("kubectl delete clusterrole clickhouse-operator-posthog --ignore-not-found")
    for namespace in namespaces:
        patch = '{"metadata":{"finalizers":null}}'
        exec_subprocess(f"kubectl patch chi posthog -n {namespace} -p '{patch}' --type=merge", ignore_errors=True)
        exec_subprocess(f"kubectl delete chi posthog -n {namespace} --ignore-not-found", ignore_errors=True)
        exec_subprocess(f"kubectl delete all --all -n {namespace}")

    log.debug("✅ Done!")


def cleanup_helm(namespaces=[NAMESPACE]):
    log.debug("🔄 Making sure helm releases get removed...")
    for namespace in namespaces:
        exec_subprocess(f"helm uninstall posthog --namespace {namespace} || true")
    log.debug("✅ Done!")


def helm_install(HELM_INSTALL_CMD):
    log.debug("🔄 Deploying PostHog...")
    exec_subprocess(HELM_INSTALL_CMD)
    log.debug("✅ Done!")


def install_chart(values, namespace=NAMESPACE):
    log.debug("🔄 Deploying PostHog...")

    values_yaml = values if isinstance(values, str) else yaml.dump(values)
    with tempfile.NamedTemporaryFile(mode="w") as values_file:
        values_file.write(values_yaml)
        values_file.flush()

        exec_subprocess(
            f"""
            helm upgrade \
                --install \
                -f {values_file.name} \
                --set cloud=local \
                --timeout 30m \
                --create-namespace \
                --namespace {namespace} \
                posthog ../../charts/posthog \
                --wait-for-jobs \
                --wait
        """
        )
    log.debug("✅ Done!")


def kubectl_exec(pod, command):
    log.debug(f"🔄 Executing command '{command}' in pod {pod}")
    cmd_run = exec_subprocess(f"kubectl exec {pod} --namespace {NAMESPACE} -- {command}")
    log.debug("✅ Done!")

    return cmd_run.stdout


def wait_for_pods_to_be_ready(kube):
    log.debug("🔄 Waiting for all pods to be ready...")
    time.sleep(30)
    start = time.time()
    timeout = 60
    while time.time() < start + timeout:
        pods = kube.get_pods(namespace="posthog")
        for pod in pods.values():
            if not pod.is_ready():
                continue
        break
    else:
        pytest.fail("❌ Timeout raised while waiting for pods to be ready")
    log.debug("✅ Done!")


def get_clickhouse_statefulset_spec(kube):
    statefulsets = kube.get_statefulsets(
        namespace="posthog",
        labels={"clickhouse.altinity.com/namespace": "posthog"},
    )
    statefulset = next(iter(statefulsets.values()))
    return statefulset.obj.spec


def get_clickhouse_cluster_service_spec(kube):
    services = kube.get_services(
        namespace="posthog",
        labels={
            "clickhouse.altinity.com/namespace": "posthog",
            "clickhouse.altinity.com/Service": "cluster",
        },
    )
    service = next(iter(services.values()))
    return service.obj.spec


def is_posthog_healthy(kube):
    test_if_posthog_deployments_are_healthy(kube)


def test_if_posthog_deployments_are_healthy(kube):
    deployments = kube.get_deployments(
        namespace="posthog",
        labels={"app": "posthog"},
    )
    for deployment_name, deployment in deployments.items():
        # Skip PgBouncer as it's a "snowflake" deployment
        # we should use a 3rd party chart for it
        if "pgbouncer" in deployment_name:
            continue
        assert deployment.is_ready(), "Deployment {} is not ready".format(deployment_name)

        pods = deployment.get_pods()
        for pod in pods:
            assert pod.is_ready(), "Pod {} of deployment {} is not ready".format(pod.name, deployment_name)


def is_prometheus_exporter_healthy(kube, exporter_name, expected_string):
    # TODO: we currently can't use the code commented below due to a possible bug
    # in `kubetest` as no pods gets returned by `get_pods()`.
    # Looks similar to https://github.com/vapor-ware/kubetest/pull/145 but I didn't
    # have time to investigate.
    #
    # deployments = kube.get_deployments(namespace="posthog")
    # deployment = deployments.get(deployment)
    # assert deployment is not None
    #
    # pods = deployment.get_pods()
    pods = kube.get_pods(namespace=NAMESPACE, labels={"app": exporter_name})
    assert len(pods) == 1, "{} deployment should have one pod".format(exporter_name)
    for pod in pods.values():
        containers = pod.get_containers()
        assert len(containers) == 1, "{} should have one container".format(exporter_name)
        resp = pod.http_proxy_get("/metrics")
        assert expected_string in resp.data


def create_namespace_if_not_exists(name="posthog"):
    log.debug("🔄 Creating namespace {} (if not exists)...".format(name))
    cmd = "kubectl create namespace {} --dry-run=client -o yaml | kubectl apply -f -".format(name)
    exec_subprocess(cmd)
    log.debug("✅ Done!")


def install_custom_resources(filename, namespace="posthog"):
    log.debug("🔄 Setting up custom resources for this test...")
    cmd = "kubectl apply -n {namespace} -f {filename}".format(namespace=namespace, filename=filename)
    exec_subprocess(cmd)
    log.debug("✅ Done!")


def install_external_statsd(namespace="posthog"):
    log.debug("🔄 Setting up external statsd...")
    cmd = """
          helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && \
          helm upgrade --install \
            --namespace {namespace} \
            external prometheus-community/prometheus-statsd-exporter \
            --version "0.4.2"
        """.format(
        namespace=namespace
    )
    exec_subprocess(cmd)
    log.debug("✅ Done!")


def exec_subprocess(cmd, ignore_errors=False):
    cmd_run = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    cmd_return_code = cmd_run.returncode
    if cmd_return_code and not ignore_errors:
        pytest.fail(
            f"""
        ❌ Error while running '{cmd}'.
        Return code: {cmd_return_code}

        STDOUT: {cmd_run.stdout}
        STDERR: {cmd_run.stderr}
        """
        )
    return cmd_run
