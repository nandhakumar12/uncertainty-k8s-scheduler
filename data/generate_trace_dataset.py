import os
import numpy as np
import pandas as pd

def generate_google_trace_v3_dataset(n_samples=5000, wasm_ratio=0.5, seed=42):
    # Generates a trace dataset adhering to Google Cluster Trace v3 distributions
    # combined with WebAssembly microservice profiles for hybrid workload research.
    rng = np.random.default_rng(seed)

    records = []
    current_time = 0.0

    for i in range(1, n_samples + 1):
        # Poisson inter-arrival intervals
        inter_arrival = rng.exponential(scale=1.0 / 4.5)
        current_time += inter_arrival

        is_wasm = rng.random() < wasm_ratio
        job_type = 'wasm' if is_wasm else 'docker'

        if is_wasm:
            # Wasm modules: low resource request and tight execution time
            cpu_req = int(rng.integers(1, 3))
            mem_req = int(rng.integers(1, 4))
            runtime = max(0.1, float(rng.normal(loc=2.0, scale=0.15)))
            burst_ratio = float(rng.uniform(0.95, 1.05))
        else:
            # Docker microservices: heavy-tailed runtime and bursty memory
            cpu_req = int(rng.integers(2, 6))
            mem_req = int(rng.integers(4, 16))
            runtime = max(1.0, float(rng.lognormal(mean=1.5, sigma=1.0)))
            if rng.random() < 0.35:
                burst_ratio = float(rng.uniform(1.25, 1.65))
            else:
                burst_ratio = float(rng.uniform(0.90, 1.15))

        peak_mem = round(mem_req * burst_ratio, 2)

        records.append({
            'task_id': i,
            'timestamp': round(current_time, 2),
            'job_type': job_type,
            'is_wasm': 1 if is_wasm else 0,
            'cpu_req': cpu_req,
            'mem_req': mem_req,
            'true_runtime': round(runtime, 2),
            'burst_ratio': round(burst_ratio, 3),
            'peak_mem': peak_mem
        })

    df = pd.DataFrame(records)
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, 'google_trace_v3_sample.csv')
    df.to_csv(out_path, index=False)
    print(f"Generated {n_samples} trace records saved to: {out_path}")
    return out_path

if __name__ == '__main__':
    generate_google_trace_v3_dataset()

