"""
Real Kubernetes Scheduler Daemon — UKS (Uncertainty-Aware Scheduler)

Watches for pods tagged with schedulerName: uks-scheduler,
runs the BNN to estimate memory uncertainty, and binds each pod
to the safest node with enough headroom.
"""

import time
import logging
import sys
import os

from kubernetes import client, config, watch
from src.schedulers import UncertaintyAwareScheduler
from src.bnn_model import BayesianSchedulerPredictor
from src.data_loader import Job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [UKS] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("uks-scheduler")

SCHEDULER_NAME = "uks-scheduler"


def load_k8s_config():
    # Try in-cluster config first (running inside a pod), fall back to local kubeconfig
    try:
        config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        log.info("Loaded local kubeconfig")


def get_node_resources(v1):
    # Fetch all schedulable nodes and their allocatable memory
    nodes = v1.list_node().items
    node_info = {}
    for node in nodes:
        # Skip the control-plane node — don't schedule workloads there
        labels = node.metadata.labels or {}
        if "node-role.kubernetes.io/control-plane" in labels:
            continue

        name = node.metadata.name
        allocatable = node.status.allocatable or {}

        # Parse memory string like "15928Mi" or "7964Ki" into MB
        mem_str = allocatable.get("memory", "0Ki")
        mem_mb = parse_memory_to_mb(mem_str)

        is_wasm = labels.get("kubernetes.io/runtime-wasm", "false") == "true"
        node_info[name] = {"mem_mb": mem_mb, "is_wasm": is_wasm}

    return node_info


def parse_memory_to_mb(mem_str):
    # Convert Kubernetes memory strings (Ki, Mi, Gi) to MB
    mem_str = mem_str.strip()
    if mem_str.endswith("Ki"):
        return int(mem_str[:-2]) / 1024
    elif mem_str.endswith("Mi"):
        return int(mem_str[:-2])
    elif mem_str.endswith("Gi"):
        return int(mem_str[:-2]) * 1024
    elif mem_str.isdigit():
        return int(mem_str) / (1024 * 1024)
    return 0.0


def get_node_used_memory(v1, node_name):
    # Sum memory requests from all running pods on this node
    pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
    used_mb = 0.0
    for pod in pods:
        if pod.status.phase not in ("Running", "Pending"):
            continue
        for container in (pod.spec.containers or []):
            req = container.resources.requests or {}
            mem_str = req.get("memory", "0Mi")
            used_mb += parse_memory_to_mb(mem_str)
    return used_mb


def build_job_features(pod):
    # Extract features from pod resource requests for the BNN predictor
    containers = pod.spec.containers or []
    total_cpu_req = 0.0
    total_mem_req = 0.0

    for c in containers:
        req = c.resources.requests or {}
        # CPU in millicores
        cpu_str = req.get("cpu", "100m")
        if cpu_str.endswith("m"):
            total_cpu_req += int(cpu_str[:-1]) / 1000.0
        else:
            total_cpu_req += float(cpu_str)
        total_mem_req += parse_memory_to_mb(req.get("memory", "64Mi"))

    labels = pod.metadata.labels or {}
    is_wasm = labels.get("runtime") == "wasm"
    job_type = "wasm" if is_wasm else "docker"

    # Return a Job object so the BNN predictor can read .job_type, .cpu_req, .mem_req
    return Job(
        job_id=0,
        job_type=job_type,
        cpu_req=total_cpu_req,
        mem_req=total_mem_req,
        true_runtime=0.0
    )


def pick_best_node(scheduler, predictor, pod, node_info, v1):
    # Score each candidate node and return the best one
    job = build_job_features(pod)
    mu, sigma = predictor.predict(job)

    labels = pod.metadata.labels or {}
    wants_wasm = labels.get("runtime") == "wasm"

    best_node = None
    best_score = float("-inf")

    for node_name, info in node_info.items():
        # Respect node affinity — wasm pods go to wasm nodes only
        if wants_wasm and not info["is_wasm"]:
            continue
        if not wants_wasm and info["is_wasm"]:
            continue

        used_mb = get_node_used_memory(v1, node_name)
        free_mb = info["mem_mb"] - used_mb

        # Score using the UKS risk formula
        # Higher free memory and lower uncertainty → higher score
        score = scheduler._score_node(free_mb, mu, sigma)

        log.info(
            f"  Node {node_name}: free={free_mb:.0f}MB mu={mu:.1f} sigma={sigma:.2f} score={score:.3f}"
        )

        if score > best_score:
            best_score = score
            best_node = node_name

    return best_node


def bind_pod_to_node(v1, pod, node_name):
    # Tell Kubernetes to assign this pod to the chosen node
    binding = client.V1Binding(
        metadata=client.V1ObjectMeta(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace
        ),
        target=client.V1ObjectReference(
            api_version="v1",
            kind="Node",
            name=node_name
        )
    )
    try:
        v1.create_namespaced_binding(
            namespace=pod.metadata.namespace,
            body=binding,
            _preload_content=False
        )
        log.info(f"Bound pod {pod.metadata.name} → {node_name} (score-based)")
    except Exception as e:
        log.error(f"Failed to bind {pod.metadata.name}: {e}")


def main():
    load_k8s_config()

    v1 = client.CoreV1Api()

    # Initialise the BNN predictor (trains once at startup)
    log.info("Initialising BNN predictor from Google Cluster Trace data...")
    predictor = BayesianSchedulerPredictor()
    log.info("BNN ready.")

    # UKS scheduler instance for scoring nodes
    scheduler = UncertaintyAwareScheduler(alpha=1.0, beta=2.5, gamma=0.8, burst_k=3.5)

    log.info(f"UKS scheduler watching for pods with schedulerName={SCHEDULER_NAME}")

    w = watch.Watch()

    while True:
        try:
            for event in w.stream(v1.list_pod_for_all_namespaces, timeout_seconds=60):
                pod = event["object"]

                # Only handle pods assigned to this scheduler that aren't placed yet
                if pod.spec.scheduler_name != SCHEDULER_NAME:
                    continue
                if pod.spec.node_name:
                    continue
                if pod.status.phase and pod.status.phase != "Pending":
                    continue

                pod_name = pod.metadata.name
                namespace = pod.metadata.namespace
                log.info(f"New pod to schedule: {namespace}/{pod_name}")

                node_info = get_node_resources(v1)
                if not node_info:
                    log.warning("No schedulable nodes found — skipping")
                    continue

                chosen_node = pick_best_node(scheduler, predictor, pod, node_info, v1)

                if chosen_node:
                    log.info(f"Decision: {pod_name} → {chosen_node}")
                    bind_pod_to_node(v1, pod, chosen_node)
                else:
                    log.warning(f"No suitable node found for {pod_name} — leaving pending")

        except Exception as e:
            log.error(f"Watch loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
