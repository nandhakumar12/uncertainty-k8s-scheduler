import simpy
import numpy as np
from src.bnn_model import BayesianSchedulerPredictor
from src.data_loader import WorkloadSynthesizer

class Node:
    # Represents one node in the cluster with its CPU/memory and live usage.
    def __init__(self, env, node_id, cpu, mem, tier="cloud"):
        self.env = env
        self.node_id = node_id
        self.cpu = cpu
        self.mem = mem
        self.tier = tier  # 'cloud', 'worker', or 'edge'

        # Kubernetes-style reserved capacity (from job requests)
        self.cpu_used = 0
        self.mem_used = 0

        # Actual runtime consumption (can exceed requests during bursts)
        self.actual_cpu_used = 0
        self.actual_mem_used = 0

        self.active_jobs = []

    def can_fit(self, req_cpu, req_mem):
        # Standard K8s filter check — does the node have enough reserved space?
        return (self.cpu - self.cpu_used >= req_cpu) and (self.mem - self.mem_used >= req_mem)

    def allocate(self, job):
        # Reserves capacity when a job is scheduled onto this node
        self.cpu_used += job.cpu_req
        self.mem_used += job.mem_req
        self.active_jobs.append(job)

    def deallocate(self, job):
        # Frees capacity when a job finishes or gets killed
        self.cpu_used -= job.cpu_req
        self.mem_used -= job.mem_req
        if job in self.active_jobs:
            self.active_jobs.remove(job)

    def get_headroom(self):
        # Returns how much buffer is left as a fraction [0–1]
        cpu_headroom = (self.cpu - self.cpu_used) / self.cpu
        mem_headroom = (self.mem - self.mem_used) / self.mem
        return max(0.0, min(cpu_headroom, mem_headroom))

    def get_colocation_risk(self):
        # Total uncertainty from all jobs currently on this node
        return sum(getattr(j, 'assigned_sigma', 0.1) for j in self.active_jobs)


class KubernetesSimulator:
    # SimPy discrete-event simulator for the hybrid Docker/Wasm cluster.
    # Models heterogeneous nodes, memory bursts, and OOMKills.
    def __init__(self, env, scheduler, wasm_ratio=0.5, random_seed=42):
        self.env = env
        self.scheduler = scheduler
        self.bnn = BayesianSchedulerPredictor(random_seed=random_seed)
        self.synthesizer = WorkloadSynthesizer(wasm_ratio=wasm_ratio, random_seed=random_seed)
        self.rng = np.random.default_rng(random_seed)

        # Cluster topology: 2 cloud nodes, 2 workers, 1 edge node
        self.nodes = [
            Node(env, 0, cpu=32, mem=64, tier="cloud"),
            Node(env, 1, cpu=32, mem=64, tier="cloud"),
            Node(env, 2, cpu=16, mem=32, tier="worker"),
            Node(env, 3, cpu=16, mem=32, tier="worker"),
            Node(env, 4, cpu=8,  mem=16, tier="edge")
        ]

        self.total_jobs = 0
        self.jobs_completed = 0
        self.oom_kills = 0
        self.pending_rejections = 0
        self.completion_latencies = []

    def run_job(self, job, node):
        # Runs a single job: allocates node, models memory burst, checks for OOMKill
        node.allocate(job)

        # Uses trace-derived burst ratio if available, otherwise models dynamic bursts
        if hasattr(job, 'burst_ratio') and job.burst_ratio is not None and job.burst_ratio > 0:
            actual_mem = job.mem_req * job.burst_ratio
        elif job.job_type == 'wasm':
            actual_mem = job.mem_req * self.rng.uniform(0.95, 1.05)
        else:
            if self.rng.random() < 0.35:
                actual_mem = job.mem_req * self.rng.uniform(1.25, 1.65)
            else:
                actual_mem = job.mem_req * self.rng.uniform(0.90, 1.15)

        node.actual_mem_used += actual_mem

        # If node physical memory is exceeded, the kernel kills the job
        if node.actual_mem_used > node.mem:
            self.oom_kills += 1
            node.actual_mem_used -= actual_mem
            node.deallocate(job)
            return

        start_time = self.env.now
        yield self.env.timeout(job.true_runtime)

        node.actual_mem_used -= actual_mem
        node.deallocate(job)
        self.jobs_completed += 1
        self.completion_latencies.append(self.env.now - start_time)

    def job_arrival_process(self, total_jobs_to_run):
        # Generates jobs at Poisson intervals and hands them to the scheduler
        while self.total_jobs < total_jobs_to_run:
            job = self.synthesizer.generate_job()
            self.total_jobs += 1

            mu, sigma = self.bnn.predict(job)
            job.predicted_mu = mu
            job.assigned_sigma = sigma

            selected_node = self.scheduler.schedule(job, self.nodes, self.bnn)

            if selected_node:
                self.env.process(self.run_job(job, selected_node))
            else:
                self.pending_rejections += 1

            inter_arrival = self.synthesizer.get_arrival_interval(rate=4.5)
            yield self.env.timeout(inter_arrival)
