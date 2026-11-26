# UKS Project — Configuration Manual

## From-Scratch Setup Guide for the Uncertainty-Aware Kubernetes Scheduler

**Author:** Nandhakumar Thangaraju (x25126067)  
**Programme:** MSc in Cloud Computing, National College of Ireland  
**Date:** November 2026

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [AWS Account Setup](#2-aws-account-setup)
3. [Terraform Infrastructure Provisioning](#3-terraform-infrastructure-provisioning)
4. [Kubernetes Cluster Bootstrap](#4-kubernetes-cluster-bootstrap)
5. [Worker Node Join Process](#5-worker-node-join-process)
6. [Flannel CNI Networking](#6-flannel-cni-networking)
7. [Wasm Worker Configuration](#7-wasm-worker-configuration)
8. [Python Environment and BNN Model](#8-python-environment-and-bnn-model)
9. [Deploying the UKS Scheduler Daemon](#9-deploying-the-uks-scheduler-daemon)
10. [Running the Benchmark Simulation](#10-running-the-benchmark-simulation)
11. [Submitting Real Jobs to the Cluster](#11-submitting-real-jobs-to-the-cluster)
12. [Troubleshooting](#12-troubleshooting)
13. [Multi-Cloud Extension (Azure / GCP)](#13-multi-cloud-extension-azure--gcp)

---

## 1. Prerequisites

Before beginning the setup, ensure the following software and accounts are available on your local development machine.

**Local machine requirements:**

| Tool | Version | Purpose |
|------|---------|---------|
| Terraform | >= 1.5.0 | Infrastructure-as-Code for AWS provisioning |
| AWS CLI v2 | >= 2.13 | Interacting with AWS services from the terminal |
| Python | >= 3.9 | Running the BNN model and benchmark simulation |
| Git | >= 2.30 | Version control and pushing to GitHub |
| VS Code (optional) | Latest | Code editing and terminal access |

**Cloud accounts:**

- AWS Academy Learner Lab account (provided by NCI). The lab provisions temporary credentials with a 4-hour session limit. The IAM role `LabRole` and instance profile `LabInstanceProfile` are pre-created and must be used for all EC2 and S3 resources since AWS Academy blocks custom IAM role creation.
- GitHub account for hosting the source code repository.

**Install Python dependencies locally:**

```bash
pip install simpy numpy pandas matplotlib scikit-learn
```

---

## 2. AWS Account Setup

### 2.1 Starting the AWS Academy Lab

1. Log in to [awsacademy.instructure.com](https://awsacademy.instructure.com).
2. Navigate to your Cloud Computing course module.
3. Click **Modules → AWS Academy Learner Lab → Start Lab**.
4. Wait for the indicator dot to turn **green** (approximately 2 minutes).
5. Click **AWS** to open the AWS Management Console.
6. Select region **us-east-1 (N. Virginia)** in the top-right corner.

### 2.2 Opening AWS CloudShell

1. In the AWS Console, click the **CloudShell** icon (bottom-left of the console).
2. CloudShell provides a browser-based terminal with the AWS CLI pre-configured using your temporary lab credentials.
3. All Terraform and kubectl commands in this guide are run from CloudShell unless stated otherwise.

### 2.3 Creating the EC2 Key Pair

```bash
aws ec2 create-key-pair \
  --key-name uks-research-keypair \
  --query 'KeyMaterial' \
  --output text > ~/uks-research-keypair.pem

chmod 400 ~/uks-research-keypair.pem
```

This key pair is used to SSH into EC2 instances from CloudShell when needed. For routine access, EC2 Instance Connect (browser-based) is simpler and does not require the .pem file.

---

## 3. Terraform Infrastructure Provisioning

### 3.1 Project Structure

The Terraform code lives in the `terraform/` directory at the root of the project:

```
terraform/
├── main.tf                # Provider and backend config
├── vpc.tf                 # VPC, subnets, internet gateway, route table
├── security_groups.tf     # Ingress/egress rules for SSH, Kubernetes API, Flannel
├── iam.tf                 # Uses pre-existing LabRole/LabInstanceProfile
├── ec2.tf                 # 4 EC2 instances (1 control plane + 2 Docker + 1 Wasm)
├── ecr.tf                 # Elastic Container Registry for scheduler images
├── s3.tf                  # S3 bucket for trace data storage
├── variables.tf           # Configurable parameters (region, instance types)
├── outputs.tf             # Prints public IPs after apply
└── user_data/
    ├── control_plane.sh   # Bootstrap script for the K8s control plane
    ├── docker_worker.sh   # Bootstrap script for Docker worker nodes
    └── wasm_worker.sh     # Bootstrap script for the Wasm worker node
```

### 3.2 Key Design Decisions

**Instance types:**

| Node | Instance Type | vCPUs | RAM | Purpose |
|------|--------------|-------|-----|---------|
| `uks-control-plane` | t3.medium | 2 | 4 GB | Kubernetes API server, etcd, kube-scheduler |
| `uks-docker-worker-01` | t3.medium | 2 | 4 GB | Runs Docker container workloads |
| `uks-docker-worker-02` | t3.medium | 2 | 4 GB | Runs Docker container workloads |
| `uks-wasm-worker-01` | t3.small | 2 | 2 GB | Runs lightweight Wasm workloads |

**Operating System:** Ubuntu 22.04 LTS (Canonical AMI, owner `099720109477`). Ubuntu 22.04 is mandatory because WasmEdge requires glibc >= 2.31, which Amazon Linux 2 does not provide.

**Networking:** A single VPC (`10.0.0.0/16`) with two subnets across different Availability Zones. The security group allows inbound SSH (port 22), Kubernetes API (port 6443), Flannel VXLAN (port 8472/UDP), and kubelet (port 10250).

### 3.3 Running Terraform

Upload the entire `terraform/` directory to CloudShell using **Actions → Upload file**, or clone from GitHub:

```bash
cd ~
git clone https://github.com/nandhakumar12/uncertainty-k8s-scheduler.git
cd uncertainty-k8s-scheduler/terraform
```

Then initialise and apply:

```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

Terraform will output the public IP addresses of all four EC2 instances. Record these for the next steps.

```
Outputs:
  control_plane_ip  = "54.81.26.149"
  docker_worker_1_ip = "54.84.163.232"
  docker_worker_2_ip = "54.235.27.254"
  wasm_worker_ip     = "54.235.11.204"
```

---

## 4. Kubernetes Cluster Bootstrap

### 4.1 Connect to the Control Plane

Use EC2 Instance Connect from the AWS Console:

1. Go to **EC2 → Instances**.
2. Select `uks-control-plane`.
3. Click **Connect → EC2 Instance Connect → Connect**.

### 4.2 Verify kubeadm Initialised

The `user_data/control_plane.sh` script should have already initialised the cluster. Verify:

```bash
kubectl get nodes -o wide
```

If the control plane is the only node listed, the initialisation succeeded. If `kubectl` is not found, the user data script may have failed — check the cloud-init log:

```bash
sudo cat /var/log/cloud-init-output.log | tail -50
```

### 4.3 Manual kubeadm Init (if user data failed)

If the cluster was not initialised automatically:

```bash
sudo kubeadm init \
  --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$(hostname -I | awk '{print $1}')

mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

---

## 5. Worker Node Join Process

### 5.1 Generate a Fresh Join Token

On the **control plane**:

```bash
kubeadm token create --print-join-command
```

This outputs a command like:

```
kubeadm join 10.0.1.243:6443 --token gl7iuk.jm99bjmid745q4xw \
  --discovery-token-ca-cert-hash sha256:502eabaed7e00ee862f285f58c288cd2ade80ada8391fd584a48af4cadc67724
```

### 5.2 Prepare Each Worker Node

Connect to each worker node using EC2 Instance Connect. On every worker, run the following kernel module and networking setup:

```bash
# Load required kernel modules for container networking
sudo modprobe overlay
sudo modprobe br_netfilter

# Persist modules across reboots
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

# Enable bridge networking and IP forwarding
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system
```

### 5.3 Configure containerd

containerd is pre-installed on Ubuntu 22.04 but needs to be configured for Kubernetes:

```bash
sudo mkdir -p /etc/containerd
sudo containerd config default | sudo tee /etc/containerd/config.toml
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
```

The `SystemdCgroup = true` setting ensures containerd uses the same cgroup driver as kubelet. Without this, kubeadm join fails with a container runtime error.

### 5.4 Install Kubernetes Components (if missing)

If the worker does not have `kubeadm` installed:

```bash
sudo apt-get update -y
sudo apt-get install -y apt-transport-https ca-certificates curl gpg

curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' | \
  sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update -y
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

### 5.5 Join the Cluster

```bash
sudo kubeadm join 10.0.1.243:6443 --token <TOKEN> \
  --discovery-token-ca-cert-hash sha256:<HASH>
```

Replace `<TOKEN>` and `<HASH>` with the values from step 5.1.

### 5.6 Verify All Nodes

Back on the control plane:

```bash
kubectl get nodes -o wide
```

Expected output:

```
NAME                    STATUS   ROLES           AGE   VERSION   INTERNAL-IP   OS-IMAGE
uks-control-plane       Ready    control-plane   8d    v1.29.0   10.0.1.243    Ubuntu 22.04.5 LTS
uks-docker-worker-17fd  Ready    <none>          8d    v1.29.0   10.0.2.49     Ubuntu 22.04.5 LTS
uks-docker-worker-2d69  Ready    <none>          8d    v1.29.0   10.0.1.197    Ubuntu 22.04.5 LTS
ip-10-0-2-64            Ready    <none>          42s   v1.29.15  10.0.2.64     Ubuntu 22.04.5 LTS
```

---

## 6. Flannel CNI Networking

Kubernetes requires a Container Network Interface (CNI) plugin for pod-to-pod communication across nodes. Without a CNI, pods remain stuck in `ContainerCreating` state.

### 6.1 Install Flannel

On the control plane:

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
```

### 6.2 Verify Flannel Pods

```bash
kubectl get pods -n kube-flannel -o wide
```

There should be one Flannel pod per node, all showing `1/1 Running`. If any pod is in `CrashLoopBackOff`, connect to that node and ensure the kernel modules from section 5.2 are loaded.

---

## 7. Wasm Worker Configuration

### 7.1 Label the Wasm Node

The Wasm worker must be labelled so that the scheduler can distinguish it from Docker workers:

```bash
kubectl label node <WASM_NODE_NAME> kubernetes.io/runtime-wasm=true
```

Replace `<WASM_NODE_NAME>` with the actual node name shown in `kubectl get nodes` (for example, `ip-10-0-2-64`).

### 7.2 Verify the Label

```bash
kubectl get nodes --show-labels | grep wasm
```

The Wasm node should show `kubernetes.io/runtime-wasm=true` in its labels column.

---

## 8. Python Environment and BNN Model

### 8.1 Project Structure

```
uncertainty-k8s-scheduler/
├── scheduler_daemon.py        # Real Kubernetes scheduler daemon
├── run_experiments.py         # SimPy benchmark (1,000 simulated jobs)
├── plot_results.py            # Generates 300 DPI publication chart
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Container packaging for the scheduler
├── data/
│   ├── generate_trace_dataset.py   # Generates synthetic trace CSV
│   └── google_trace_v3_sample.csv  # 5,000 tasks from Google Cluster Trace v3
├── src/
│   ├── bnn_model.py           # Bayesian Neural Network (MC Dropout)
│   ├── data_loader.py         # Google Cluster Trace data loader
│   ├── schedulers.py          # 3 scheduler implementations (Default, Deterministic, UKS)
│   └── simulator.py           # SimPy discrete-event cluster simulator
├── k8s-manifests/
│   ├── uks-scheduler/
│   │   ├── rbac.yaml          # RBAC permissions for the scheduler
│   │   └── deployment.yaml    # Kubernetes Deployment for the scheduler pod
│   └── test-workloads/
│       ├── docker-jobs.yaml   # Docker test jobs with schedulerName: uks-scheduler
│       └── wasm-jobs.yaml     # Wasm test jobs with schedulerName: uks-scheduler
└── results/
    └── benchmark_results.png  # Generated benchmark chart
```

### 8.2 Generate the Google Cluster Trace Dataset

The dataset is a 5,000-row CSV synthesised from the statistical distributions reported in Google Cluster Trace v3. Each row represents a task with the following columns:

| Column | Description |
|--------|-------------|
| `task_id` | Unique integer identifier |
| `job_type` | `docker` or `wasm` |
| `is_wasm` | Binary flag (0 or 1) |
| `cpu_req` | CPU cores requested |
| `mem_req` | Memory in arbitrary units requested |
| `true_runtime` | Actual runtime in seconds |
| `burst_ratio` | Ratio of peak memory to requested memory |
| `peak_mem` | Actual peak memory consumption |

```bash
cd uncertainty-k8s-scheduler
python data/generate_trace_dataset.py
```

This creates `data/google_trace_v3_sample.csv`.

### 8.3 BNN Architecture

The Bayesian Neural Network is implemented in pure NumPy (no PyTorch or TensorFlow dependency) in `src/bnn_model.py`. The key design choices:

- **Architecture:** 3 input features → 64 hidden units → 32 hidden units → 1 output
- **Activation:** ReLU
- **Weight Initialisation:** He / Kaiming normal
- **Dropout Rate:** 0.15 (applied during both training and inference)
- **Optimiser:** Adam (learning rate 0.005, 35 epochs, batch size 64)
- **Uncertainty Estimation:** Monte Carlo Dropout with 30 stochastic forward passes during inference. The mean of the 30 predictions gives the expected runtime (mu) and the standard deviation gives the epistemic uncertainty (sigma).

### 8.4 Install Dependencies on the Control Plane

```bash
sudo apt-get install -y python3-pip
cd ~/uncertainty-k8s-scheduler
pip3 install -r requirements.txt
```

---

## 9. Deploying the UKS Scheduler Daemon

The scheduler daemon (`scheduler_daemon.py`) is a Python process that runs on the control plane and acts as a custom Kubernetes scheduler. It continuously watches for pods with `schedulerName: uks-scheduler`, runs the BNN to estimate memory uncertainty, scores each candidate node, and binds the pod to the best-scoring node.

### 9.1 Direct Execution (Recommended for Testing)

```bash
cd ~/uncertainty-k8s-scheduler
python3 scheduler_daemon.py
```

Expected startup output:

```
2026-11-24 13:50:00 [UKS] Loaded local kubeconfig
2026-11-24 13:50:00 [UKS] Initialising BNN predictor from Google Cluster Trace data...
2026-11-24 13:50:03 [UKS] BNN ready.
2026-11-24 13:50:03 [UKS] UKS scheduler watching for pods with schedulerName=uks-scheduler
```

### 9.2 UKS Scoring Formula

For each unscheduled pod, the daemon evaluates every candidate node using:

**Score = alpha × FreeMemory − RiskPenalty**

Where `RiskPenalty = beta × |FreeMemory − (mu + burst_k × sigma)| × 10` if the node does not have enough headroom to absorb the worst-case memory burst.

The tuned hyperparameters are: alpha = 1.0, beta = 2.5, gamma = 0.8, burst_k = 3.5.

---

## 10. Running the Benchmark Simulation

The simulation is independent of the live cluster and can be run on any machine with Python installed.

```bash
cd uncertainty-k8s-scheduler
python run_experiments.py
```

This runs 1,000 simulated jobs through three schedulers:

1. **Default Kube-Scheduler (LeastAllocated)** — baseline.
2. **Deterministic ML Scheduler (Point mu)** — uses BNN mean only, ignores uncertainty.
3. **Uncertainty-Aware Scheduler (Proposed UKS)** — uses both mu and sigma.

Expected output:

```
Scheduler                                  | Completed | OOMKills | Pending | Fail Rate | Makespan
Default Kube-Scheduler (LeastAllocated)    | 896       | 103      | 1       |  10.40%   |   238.37
Deterministic ML Scheduler (Point mu)      | 896       | 103      | 1       |  10.40%   |   238.37
Uncertainty-Aware Scheduler (Proposed)     | 928       | 72       | 0       |   7.20%   |   238.37

[KEY RESEARCH FINDING]
* Proposed scheduler reduced OOMKills by 30.1%
```

To generate the publication chart:

```bash
python plot_results.py
```

The chart is saved to `results/benchmark_results.png` at 300 DPI.

---

## 11. Submitting Real Jobs to the Cluster

With the scheduler daemon running (section 9.1), open a second terminal on the control plane:

### 11.1 Create the Namespace

```bash
kubectl create namespace uks-research
```

### 11.2 Apply the RBAC Permissions

```bash
kubectl apply -f k8s-manifests/uks-scheduler/rbac.yaml
```

### 11.3 Submit the Test Workloads

```bash
kubectl apply -f k8s-manifests/test-workloads/docker-jobs.yaml
kubectl apply -f k8s-manifests/test-workloads/wasm-jobs.yaml
```

### 11.4 Observe Scheduler Decisions

Switch back to the terminal running the daemon. The output will show the BNN evaluating each pod and the scoring logic selecting a node:

```
[UKS] New pod to schedule: uks-research/docker-job-high-mem-xxxxx
[UKS]   Node uks-docker-worker-17fd: free=12400MB mu=128.3 sigma=18.2 score=12271.7
[UKS]   Node uks-docker-worker-2d69: free=8200MB  mu=128.3 sigma=18.2 score=8071.7
[UKS] Decision: docker-job-high-mem-xxxxx → uks-docker-worker-17fd
[UKS] Bound pod docker-job-high-mem-xxxxx → uks-docker-worker-17fd (score-based)
```

### 11.5 Verify Pod Placement

```bash
kubectl get pods -n uks-research -o wide
```

Docker jobs should appear on `uks-docker-worker-17fd` and `uks-docker-worker-2d69`. Wasm jobs should appear on `ip-10-0-2-64` (the Wasm-labelled node).

---

## 12. Troubleshooting

### Common Issues and Fixes

| Issue | Cause | Fix |
|-------|-------|-----|
| `kubeadm: command not found` on worker | Kubernetes packages not installed | Follow section 5.4 to install kubelet, kubeadm, kubectl |
| `failed to load flannel subnet.env` | br_netfilter module not loaded | Run `sudo modprobe br_netfilter` and apply sysctl (section 5.2) |
| `containerd.io: Conflicts: containerd` | docker.io conflicts with existing containerd | Do not install docker.io on nodes running kubelet. Use containerd only. |
| Pods stuck in `ContainerCreating` | Flannel CNI not installed | Run `kubectl apply -f kube-flannel.yml` (section 6.1) |
| `SystemdCgroup` error during kubeadm join | containerd misconfigured | Generate and edit containerd config (section 5.3) |
| `Permission denied (publickey)` via SSH | Key not on CloudShell, or wrong permissions | Upload .pem to CloudShell, run `chmod 400` |
| AWS Academy session expired | 4-hour lab timer ran out | Go to Learner Lab → Start Lab → wait for green dot |
| `cannot create IAM role` | AWS Academy SCP restriction | Use the pre-existing `LabRole` / `LabInstanceProfile` only |
| Docker image build fails on control plane | docker.io conflicts with containerd | Run Python scheduler directly instead (section 9.1) |

---

## 13. Multi-Cloud Extension (Azure / GCP)

The UKS scheduler is cloud-agnostic by design — it communicates only with the standard Kubernetes API (nodes, pods, bindings). It can be deployed on any cloud platform that supports Kubernetes.

### 13.1 Azure Deployment Option

| Component | Azure Service | Notes |
|-----------|--------------|-------|
| VM Instances | Azure VMs (B2s / B2ms) | Ubuntu 22.04, same kubeadm setup |
| Container Registry | Azure Container Registry (ACR) | Replace ECR with ACR |
| Networking | Azure VNet + NSG | Equivalent to VPC + Security Groups |
| Managed K8s (alternative) | AKS | Cannot easily replace scheduler in AKS |

**Key commands for Azure:**

```bash
# Create resource group
az group create --name uks-rg --location westeurope

# Create VMs
az vm create --resource-group uks-rg --name uks-control-plane \
  --image Canonical:0001-com-ubuntu-server-jammy:22_04-lts:latest \
  --size Standard_B2ms --admin-username ubuntu --generate-ssh-keys
```

### 13.2 GCP Deployment Option

| Component | GCP Service | Notes |
|-----------|------------|-------|
| VM Instances | Compute Engine (e2-medium) | Ubuntu 22.04 |
| Container Registry | Artifact Registry | Replace ECR |
| Networking | VPC + Firewall Rules | Equivalent setup |
| Managed K8s (alternative) | GKE | Custom scheduler possible but complex |

### 13.3 Multi-Cloud Architecture

A true multi-cloud deployment would have worker nodes across different cloud providers, all joining the same Kubernetes control plane. The UKS scheduler would then make placement decisions considering not just memory uncertainty but also cross-cloud latency and cost.

This extension is documented as future work in the dissertation.

---

*End of Configuration Manual*
