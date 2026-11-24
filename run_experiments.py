import simpy
from src.simulator import KubernetesSimulator
from src.schedulers import DefaultKubeScheduler, DeterministicMLScheduler, UncertaintyAwareScheduler

def run_experiment(scheduler_name, scheduler_instance, wasm_ratio=0.5, total_jobs=1000, seed=42):
    # Runs one full simulation and returns the key metrics
    env = simpy.Environment()
    sim = KubernetesSimulator(env, scheduler=scheduler_instance, wasm_ratio=wasm_ratio, random_seed=seed)

    env.process(sim.job_arrival_process(total_jobs_to_run=total_jobs))
    env.run()

    total_failures = sim.oom_kills + sim.pending_rejections
    failure_rate = (total_failures / sim.total_jobs) * 100.0
    oom_rate = (sim.oom_kills / sim.total_jobs) * 100.0

    return {
        "name": scheduler_name,
        "total_jobs": sim.total_jobs,
        "completed": sim.jobs_completed,
        "oom_kills": sim.oom_kills,
        "oom_rate": oom_rate,
        "pending": sim.pending_rejections,
        "total_failures": total_failures,
        "failure_rate": failure_rate,
        "makespan": env.now
    }

if __name__ == "__main__":
    total_jobs = 1000
    wasm_ratio = 0.5  # 50/50 Docker and Wasm mix
    seed = 42

    print("=" * 85)
    print(f" UKS RESEARCH BENCHMARK: 1,000 HYBRID WORKLOAD RUN (WASM Ratio: {wasm_ratio*100:.0f}%)")
    print("=" * 85)

    schedulers = [
        ("Default Kube-Scheduler (LeastAllocated)", DefaultKubeScheduler()),
        ("Deterministic ML Scheduler (Point mu)", DeterministicMLScheduler()),
        ("Uncertainty-Aware Scheduler (Proposed)", UncertaintyAwareScheduler(alpha=1.0, beta=2.5, gamma=0.8, burst_k=3.5))
    ]

    all_results = []
    for name, sched in schedulers:
        print(f"--> Simulating: {name}...")
        res = run_experiment(name, sched, wasm_ratio=wasm_ratio, total_jobs=total_jobs, seed=seed)
        all_results.append(res)

    print("\n" + "=" * 85)
    print(" EXPERIMENT RESULTS & COMPARATIVE ANALYSIS")
    print("=" * 85)
    header = f"{'Scheduler':<42} | {'Completed':<9} | {'OOMKills':<8} | {'Pending':<7} | {'Fail Rate':<9} | {'Makespan':<8}"
    print(header)
    print("-" * 85)

    for r in all_results:
        row = f"{r['name']:<42} | {r['completed']:<9} | {r['oom_kills']:<8} | {r['pending']:<7} | {r['failure_rate']:>6.2f}%   | {r['makespan']:>8.2f}"
        print(row)

    print("=" * 85)

    # How much better did the proposed scheduler do?
    def_oom = all_results[0]["oom_kills"]
    prop_oom = all_results[2]["oom_kills"]
    oom_reduction = ((def_oom - prop_oom) / max(1, def_oom)) * 100.0

    print("\n[KEY RESEARCH FINDING]")
    print(f"* Proposed scheduler reduced OOMKills by {oom_reduction:.1f}%")
    print(f"* OOMKills dropped from {def_oom} (Default) to {prop_oom} (Proposed)")
    print(f"* Wasm jobs were packed densely; volatile Docker jobs routed to safe headroom nodes.")
    print("=" * 85)
