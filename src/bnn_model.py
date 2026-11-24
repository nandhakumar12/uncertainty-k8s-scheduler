import os
import numpy as np

# Bayesian Neural Network (BNN) for Workload Runtime and Uncertainty Estimation.
# Implements Monte Carlo Dropout (Gal & Ghahramani, ICML 2016).
# Uses stochastic dropout during both training and inference.
# Multiple forward passes approximate variational inference to produce
# the predictive mean (mu) and model uncertainty (sigma).

class BayesianNeuralNetwork:
    # 2-layer Bayesian Neural Network trained with Adam and Monte Carlo Dropout
    def __init__(self, in_features=3, hidden1=64, hidden2=32, dropout_rate=0.15, seed=42):
        self.rng = np.random.default_rng(seed)
        self.dropout_rate = dropout_rate
        self.is_trained = False

        # He / Kaiming normal weight initialization
        self.W1 = self.rng.normal(0, np.sqrt(2.0 / in_features), (in_features, hidden1)).astype(np.float32)
        self.b1 = np.zeros((1, hidden1), dtype=np.float32)
        self.W2 = self.rng.normal(0, np.sqrt(2.0 / hidden1), (hidden1, hidden2)).astype(np.float32)
        self.b2 = np.zeros((1, hidden2), dtype=np.float32)
        self.W3 = self.rng.normal(0, np.sqrt(2.0 / hidden2), (hidden2, 1)).astype(np.float32)
        self.b3 = np.zeros((1, 1), dtype=np.float32)

        self.params = [self.W1, self.b1, self.W2, self.b2, self.W3, self.b3]
        self.x_mean = None
        self.x_std = None

    def fit(self, X, y, epochs=40, batch_size=64, lr=0.005):
        # Trains network weights using Adam optimizer on historical trace data
        X = np.array(X, dtype=np.float32)
        y = np.array(y, dtype=np.float32).reshape(-1, 1)

        # Standardize features for stable gradient descent
        self.x_mean = np.mean(X, axis=0, keepdims=True)
        self.x_std = np.std(X, axis=0, keepdims=True) + 1e-6
        X_norm = (X - self.x_mean) / self.x_std

        # Adam optimizer state
        m = [np.zeros_like(p) for p in self.params]
        v = [np.zeros_like(p) for p in self.params]
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        t = 0

        n_batches = max(1, len(X_norm) // batch_size)

        for epoch in range(epochs):
            indices = self.rng.permutation(len(X_norm))
            X_shuffled = X_norm[indices]
            y_shuffled = y[indices]

            for b in range(n_batches):
                t += 1
                xb = X_shuffled[b * batch_size:(b + 1) * batch_size]
                yb = y_shuffled[b * batch_size:(b + 1) * batch_size]
                curr_bs = len(xb)

                # Forward pass with inverted dropout
                z1 = xb @ self.W1 + self.b1
                a1 = np.maximum(0, z1)
                mask1 = (self.rng.random(a1.shape) > self.dropout_rate) / (1.0 - self.dropout_rate)
                a1_drop = a1 * mask1

                z2 = a1_drop @ self.W2 + self.b2
                a2 = np.maximum(0, z2)
                mask2 = (self.rng.random(a2.shape) > self.dropout_rate) / (1.0 - self.dropout_rate)
                a2_drop = a2 * mask2

                out = a2_drop @ self.W3 + self.b3

                # Mean Squared Error loss gradient
                loss_grad = 2.0 * (out - yb) / curr_bs

                # Backpropagation through layers and dropout masks
                dW3 = a2_drop.T @ loss_grad
                db3 = np.sum(loss_grad, axis=0, keepdims=True)

                da2 = (loss_grad @ self.W3.T) * mask2
                dz2 = da2 * (z2 > 0)
                dW2 = a1_drop.T @ dz2
                db2 = np.sum(dz2, axis=0, keepdims=True)

                da1 = (dz2 @ self.W2.T) * mask1
                dz1 = da1 * (z1 > 0)
                dW1 = xb.T @ dz1
                db1 = np.sum(dz1, axis=0, keepdims=True)

                grads = [dW1, db1, dW2, db2, dW3, db3]

                # Adam parameter update
                for i in range(len(self.params)):
                    m[i] = beta1 * m[i] + (1 - beta1) * grads[i]
                    v[i] = beta2 * v[i] + (1 - beta2) * (grads[i] ** 2)
                    m_hat = m[i] / (1.0 - beta1 ** t)
                    v_hat = v[i] / (1.0 - beta2 ** t)
                    self.params[i] -= lr * m_hat / (np.sqrt(v_hat) + eps)

        self.is_trained = True

    def predict_distribution(self, x_raw, mc_samples=30):
        # Runs T stochastic forward passes with dropout active to evaluate (mu, sigma)
        if not self.is_trained or self.x_mean is None:
            return float(x_raw[1]), 0.5

        x_arr = np.array(x_raw, dtype=np.float32).reshape(1, -1)
        x_norm = (x_arr - self.x_mean) / self.x_std

        predictions = []
        for _ in range(mc_samples):
            # Monte Carlo sampling: dropout mask active during inference
            z1 = x_norm @ self.W1 + self.b1
            m1 = (self.rng.random(z1.shape) > self.dropout_rate) / (1.0 - self.dropout_rate)
            a1 = np.maximum(0, z1) * m1

            z2 = a1 @ self.W2 + self.b2
            m2 = (self.rng.random(z2.shape) > self.dropout_rate) / (1.0 - self.dropout_rate)
            a2 = np.maximum(0, z2) * m2

            out = a2 @ self.W3 + self.b3
            predictions.append(max(0.1, float(out[0, 0])))

        mu = float(np.mean(predictions))
        sigma = float(np.std(predictions))
        return max(0.1, mu), max(0.05, sigma)


class BayesianSchedulerPredictor:
    # Interface consumed by Kubernetes schedulers during the scoring phase.
    # Predicts expected duration (mu) and model uncertainty (sigma) from job requests.
    def __init__(self, random_seed=42):
        self.random_seed = random_seed
        self.bnn = BayesianNeuralNetwork(seed=random_seed)
        self._auto_train_from_trace()

    def _auto_train_from_trace(self):
        # Automatically trains the BNN on Google Cluster Trace CSV if available
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        csv_path = os.path.join(base_dir, 'data', 'google_trace_v3_sample.csv')

        if os.path.exists(csv_path):
            from src.data_loader import GoogleTraceLoader
            loader = GoogleTraceLoader(csv_path=csv_path, random_seed=self.random_seed)
            X, y = loader.get_training_data(train_size=3000)
            if X is not None and len(X) > 0:
                self.bnn.fit(X, y, epochs=35, batch_size=64, lr=0.005)

    def predict(self, job):
        # Maps job attributes [cpu_req, mem_req, is_wasm] to BNN predictive distribution
        is_wasm = 1.0 if job.job_type == 'wasm' else 0.0
        x_feat = [float(job.cpu_req), float(job.mem_req), is_wasm]
        return self.bnn.predict_distribution(x_feat, mc_samples=30)
