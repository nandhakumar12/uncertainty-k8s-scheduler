import os
import matplotlib.pyplot as plt
from run_experiments import run_experiment
from src.schedulers import DefaultKubeScheduler, DeterministicMLScheduler, UncertaintyAwareScheduler

def generate_benchmark_plots():
    # Generates dissertation-ready comparison charts for the 3 schedulers
    total_jobs = 1000
    wasm_ratio = 0.5
    seed = 42

    schedulers = [
        ("Default K8s\n(LeastAllocated)", DefaultKubeScheduler()),
        ("Deterministic ML\n(Point mu)", DeterministicMLScheduler()),
        ("Proposed UKS\n(Uncertainty-Aware)", UncertaintyAwareScheduler(alpha=1.0, beta=2.5, gamma=0.8, burst_k=3.5))
    ]

    names = []
    oom_kills = []
    completed = []
    failure_rates = []

    for label, sched in schedulers:
        res = run_experiment(label, sched, wasm_ratio=wasm_ratio, total_jobs=total_jobs, seed=seed)
        names.append(label)
        oom_kills.append(res["oom_kills"])
        completed.append(res["completed"])
        failure_rates.append(res["failure_rate"])

    # Setup publication-style 2-panel chart
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5), dpi=300)
    plt.subplots_adjust(wspace=0.35)

    colors = ['#7f8c8d', '#e67e22', '#27ae60']

    # Chart 1: OOMKills
    bars1 = ax1.bar(names, oom_kills, color=colors, width=0.55, edgecolor='black', linewidth=0.8)
    ax1.set_ylabel("OOMKill Evictions (Lower is Better)", fontsize=11, fontweight='bold')
    ax1.set_title("OOMKill Reduction Comparison", fontsize=12, fontweight='bold')
    ax1.grid(axis='y', linestyle='--', alpha=0.5)

    for bar in bars1:
        yval = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width()/2.0, yval + 1.5, f"{int(yval)}", ha='center', va='bottom', fontweight='bold')

    # Chart 2: Failure Rate
    bars2 = ax2.bar(names, failure_rates, color=colors, width=0.55, edgecolor='black', linewidth=0.8)
    ax2.set_ylabel("Total Failure Rate % (Lower is Better)", fontsize=11, fontweight='bold')
    ax2.set_title("Task Failure Rate Comparison", fontsize=12, fontweight='bold')
    ax2.grid(axis='y', linestyle='--', alpha=0.5)

    for bar in bars2:
        yval = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2.0, yval + 0.15, f"{yval:.1f}%", ha='center', va='bottom', fontweight='bold')

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "benchmark_results.png")
    plt.savefig(out_file, bbox_inches='tight')
    plt.close()
    print(f"Chart saved successfully to: {out_file}")

if __name__ == '__main__':
    generate_benchmark_plots()
