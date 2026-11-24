import os
import numpy as np
import pandas as pd

class Job:
    def __init__(self, job_id, job_type, cpu_req, mem_req, true_runtime, burst_ratio=1.0):
        self.job_id = job_id
        self.job_type = job_type  # 'docker' or 'wasm'
        self.cpu_req = cpu_req
        self.mem_req = mem_req
        self.true_runtime = true_runtime
        self.burst_ratio = burst_ratio

class GoogleTraceLoader:
    # Loads and parses task events from Google Cluster Trace v3.
    # Streams tasks for simulation and provides train/val data for the BNN.
    def __init__(self, csv_path=None, wasm_ratio=0.5, random_seed=42):
        self.wasm_ratio = wasm_ratio
        self.rng = np.random.default_rng(random_seed)
        self.job_counter = 0

        # Look for trace CSV if path is not specified
        if csv_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            csv_path = os.path.join(base_dir, 'data', 'google_trace_v3_sample.csv')

        self.csv_path = csv_path
        self.df = None
        self.current_idx = 0

        if os.path.exists(self.csv_path):
            self.df = pd.read_csv(self.csv_path)

    def generate_job(self):
        # Reads the next task from the trace, or synthesizes if end of file
        self.job_counter += 1

        if self.df is not None and self.current_idx < len(self.df):
            row = self.df.iloc[self.current_idx]
            self.current_idx += 1
            return Job(
                job_id=int(row['task_id']),
                job_type=str(row['job_type']),
                cpu_req=int(row['cpu_req']),
                mem_req=int(row['mem_req']),
                true_runtime=float(row['true_runtime']),
                burst_ratio=float(row.get('burst_ratio', 1.0))
            )

        # Fallback synthesis if CSV is missing or exhausted
        is_wasm = self.rng.random() < self.wasm_ratio
        job_type = 'wasm' if is_wasm else 'docker'

        if job_type == 'wasm':
            cpu_req = int(self.rng.integers(1, 3))
            mem_req = int(self.rng.integers(1, 4))
            true_runtime = max(0.1, float(self.rng.normal(loc=2.0, scale=0.15)))
            burst_ratio = float(self.rng.uniform(0.95, 1.05))
        else:
            cpu_req = int(self.rng.integers(2, 6))
            mem_req = int(self.rng.integers(4, 16))
            true_runtime = max(1.0, float(self.rng.lognormal(mean=1.5, sigma=1.0)))
            if self.rng.random() < 0.35:
                burst_ratio = float(self.rng.uniform(1.25, 1.65))
            else:
                burst_ratio = float(self.rng.uniform(0.90, 1.15))

        return Job(self.job_counter, job_type, cpu_req, mem_req, true_runtime, burst_ratio)

    def get_arrival_interval(self, rate=4.5):
        # Exponential inter-arrival times for a Poisson process
        return self.rng.exponential(scale=1.0 / rate)

    def get_training_data(self, train_size=2000):
        # Extracts feature matrix X and target vector y for BNN model training
        if self.df is None or len(self.df) == 0:
            return None, None

        sample_df = self.df.iloc[:train_size]
        X = sample_df[['cpu_req', 'mem_req', 'is_wasm']].values.astype(np.float32)
        y = sample_df['true_runtime'].values.astype(np.float32)
        return X, y

# Alias for compatibility with simulator
WorkloadSynthesizer = GoogleTraceLoader
