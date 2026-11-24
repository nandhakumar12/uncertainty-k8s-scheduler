class BaseScheduler:
    def schedule(self, job, nodes, bnn_predictor=None):
        raise NotImplementedError


class DefaultKubeScheduler(BaseScheduler):
    # Standard Kubernetes LeastAllocated — picks the node with most free CPU + memory.
    # Ignores prediction uncertainty entirely, so volatile Docker jobs cause OOMKills.
    def schedule(self, job, nodes, bnn_predictor=None):
        best_node = None
        best_score = -float('inf')

        for node in nodes:
            if node.can_fit(job.cpu_req, job.mem_req):
                free_cpu = node.cpu - node.cpu_used
                free_mem = node.mem - node.mem_used
                score = free_cpu + free_mem

                if score > best_score:
                    best_score = score
                    best_node = node

        return best_node


class DeterministicMLScheduler(BaseScheduler):
    # Uses predicted mean runtime (mu) to score nodes but ignores variance.
    # Works okay on average but badly underestimates heavy-tailed Docker jobs.
    def schedule(self, job, nodes, bnn_predictor):
        best_node = None
        best_score = -float('inf')

        mu, _ = bnn_predictor.predict(job)

        for node in nodes:
            if node.can_fit(job.cpu_req, job.mem_req):
                free_resources = (node.cpu - node.cpu_used) + (node.mem - node.mem_used)
                score = free_resources / max(0.1, mu)

                if score > best_score:
                    best_score = score
                    best_node = node

        return best_node


class UncertaintyAwareScheduler(BaseScheduler):
    # Proposed scheduler using Bayesian Neural Network runtime and uncertainty predictions.
    # Phase 1 filters nodes capable of absorbing worst-case memory bursts.
    # Phase 2 scores candidates using headroom reward, burst deficit penalty, and co-location risk.
    def __init__(self, alpha=1.0, beta=2.5, gamma=0.8, burst_k=3.5):
        self.alpha = alpha      # weight for free resources
        self.beta = beta        # penalty when burst exceeds free memory
        self.gamma = gamma      # penalty for stacking uncertain jobs together
        self.burst_k = burst_k  # confidence multiplier for BNN predictive uncertainty

    def schedule(self, job, nodes, bnn_predictor):
        best_node = None
        best_score = -float('inf')

        mu, sigma = bnn_predictor.predict(job)
        burst_safety_buffer = self.burst_k * sigma

        # Phase 1: separate nodes that safely absorb the burst from the rest
        candidate_nodes = []
        for node in nodes:
            if node.can_fit(job.cpu_req, job.mem_req):
                free_mem = node.mem - node.mem_used
                has_safety_buffer = (free_mem >= (job.mem_req + burst_safety_buffer))
                candidate_nodes.append((node, has_safety_buffer))

        safe_candidates = [n for n, is_safe in candidate_nodes if is_safe]
        eligible_nodes = safe_candidates if safe_candidates else [n for n, _ in candidate_nodes]

        # Phase 2: score eligible nodes
        for node in eligible_nodes:
            free_mem = node.mem - node.mem_used
            free_cpu = node.cpu - node.cpu_used
            headroom = node.get_headroom()
            colocation_risk = node.get_colocation_risk()

            remaining_after_burst = free_mem - (job.mem_req + burst_safety_buffer)
            if remaining_after_burst < 0:
                risk_penalty = self.beta * abs(remaining_after_burst) * 10.0
            else:
                risk_penalty = 0.0

            score = (self.alpha * (free_cpu + free_mem * 2.0)) - risk_penalty - (self.gamma * colocation_risk)

            if score > best_score:
                best_score = score
                best_node = node

        return best_node
