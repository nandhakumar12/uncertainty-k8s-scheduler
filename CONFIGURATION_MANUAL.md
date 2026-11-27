# Configuration Manual — UKS Project Setup

**Nandhakumar Thangaraju — x25126067**  
**MSc Cloud Computing, National College of Ireland**  
**November 2026**

---

## What this document covers

This manual walks through how I set up my Uncertainty-Aware Kubernetes Scheduler (UKS) research project from scratch. It covers everything — the AWS infrastructure, the Kubernetes cluster, the Python scheduler code, and running real workloads on it. If someone needs to recreate this setup or understand how it all fits together, this is the guide.

---

## 1. What I needed before starting

On my local Windows laptop I had these installed:

- Python 3.9 (from python.org)
- Git (for pushing code to GitHub)
- VS Code (for editing code and running terminal commands)
- Terraform (I downloaded version 1.5 from terraform.io)
- AWS CLI v2 (installed via the MSI installer from AWS docs)

For cloud access, I used the AWS Academy Learner Lab provided through NCI. The lab gives temporary credentials that last about 4 hours per session. One important thing I learned early on — you cannot create custom IAM roles in the Academy environment. There is a Service Control Policy (SCP) that blocks it. So every EC2 instance has to use the pre-existing `LabRole` and `LabInstanceProfile` that AWS Academy provides.

I also set up a GitHub account to host the code so I could pull it onto the cluster later.

---

## 2. Getting the AWS lab running

I logged into [awsacademy.instructure.com](https://awsacademy.instructure.com), went to my Cloud Computing course module, and clicked Modules then AWS Academy Learner Lab then Start Lab. It takes a couple of minutes for the dot to go green. Once it was green I clicked AWS to open the console.

I made sure to select **us-east-1 (N. Virginia)** as the region in the top right corner. All my resources are in that region.

For terminal access I used **AWS CloudShell** — it's the terminal icon at the bottom left of the AWS console. It already has the AWS CLI configured with the lab credentials so I didn't need to mess with access keys.

I also created a key pair for SSH access to the EC2 instances:

```bash
aws ec2 create-key-pair --key-name uks-research-keypair \
  --query 'KeyMaterial' --output text > ~/uks-research-keypair.pem
chmod 400 ~/uks-research-keypair.pem
```

In practice I ended up using EC2 Instance Connect (browser-based) most of the time because it's simpler — you just click Connect in the EC2 console and get a terminal in a new tab. No key file needed.

---

## 3. Setting up the AWS infrastructure with Terraform

I wrote Terraform files to provision everything. The files are in the `terraform/` folder in my project repo. Here is what each file does:

- `main.tf` — sets the AWS provider and region
- `vpc.tf` — creates a VPC (10.0.0.0/16) with two subnets in different availability zones, an internet gateway, and a route table so the instances can reach the internet
- `security_groups.tf` — opens port 22 (SSH), port 6443 (Kubernetes API), port 8472 (Flannel VXLAN overlay), and port 10250 (kubelet)
- `iam.tf` — references the existing LabRole and LabInstanceProfile (does not try to create new ones since Academy blocks that)
- `ec2.tf` — creates 4 EC2 instances
- `ecr.tf` — creates an Elastic Container Registry repo called `uks-scheduler-workloads`
- `s3.tf` — creates an S3 bucket for storing trace data
- `variables.tf` — has configurable stuff like region and instance types
- `outputs.tf` — prints the public IPs after the apply finishes

The 4 EC2 instances are:

| Name | Type | RAM | What it does |
|------|------|-----|-------------|
| uks-control-plane | t3.medium | 4 GB | Runs the Kubernetes master (API server, etcd, scheduler) |
| uks-docker-worker-01 | t3.medium | 4 GB | Runs Docker container workloads |
| uks-docker-worker-02 | t3.medium | 4 GB | Runs Docker container workloads |
| uks-wasm-worker-01 | t3.small | 2 GB | Runs lightweight Wasm workloads |

All of them run Ubuntu 22.04 LTS. I had to use Ubuntu specifically because WasmEdge needs glibc 2.31 or higher, and Amazon Linux 2 only has 2.26. The AMI comes from Canonical (owner ID `099720109477`).

Each instance has a user data script that runs on first boot (`user_data/control_plane.sh`, `docker_worker.sh`, `wasm_worker.sh`). These scripts install containerd and Kubernetes packages automatically.

To deploy everything I ran:

```bash
cd ~/uncertainty-k8s-scheduler/terraform
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

After it finished I got the public IPs for all 4 instances.

---

## 4. Bootstrapping the Kubernetes cluster

### Setting up the control plane

I connected to the control plane using EC2 Instance Connect — in the EC2 console I clicked on the control plane instance, then Connect, then EC2 Instance Connect tab, then Connect. A terminal opened in a new browser tab.

The user data script should have already run `kubeadm init`. I checked by running:

```bash
kubectl get nodes -o wide
```

If it showed the control plane node as Ready, the init worked. If kubectl wasn't found or nothing showed up, I would have needed to run the init manually:

```bash
sudo kubeadm init --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$(hostname -I | awk '{print $1}')

mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

### Getting worker nodes to join

First I generated a join token on the control plane:

```bash
kubeadm token create --print-join-command
```

This gave me a command like:

```
kubeadm join 10.0.1.243:6443 --token gl7iuk.jm99bjmid745q4xw \
  --discovery-token-ca-cert-hash sha256:502eaba...
```

Then I connected to each worker node using EC2 Instance Connect and ran some setup commands before joining. This was necessary because the workers needed kernel modules and containerd configuration that the user data scripts didn't always set up properly.

On each worker I ran:

```bash
# These kernel modules are needed for container networking
sudo modprobe overlay
sudo modprobe br_netfilter

# Make them load on reboot too
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

# Turn on bridge networking and IP forwarding
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system
```

Then I configured containerd properly. This was important — without the SystemdCgroup setting, kubeadm join fails with a container runtime error:

```bash
sudo mkdir -p /etc/containerd
sudo containerd config default | sudo tee /etc/containerd/config.toml
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
```

On my Wasm worker, kubeadm wasn't even installed (the user data script had failed silently). So I had to install the Kubernetes packages manually:

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

After that, on each worker I ran the join command:

```bash
sudo kubeadm join 10.0.1.243:6443 --token gl7iuk.jm99bjmid745q4xw \
  --discovery-token-ca-cert-hash sha256:502eaba...
```

Back on the control plane I verified everything:

```bash
kubectl get nodes -o wide
```

Output:

```
NAME                    STATUS   ROLES           AGE   VERSION    INTERNAL-IP   OS-IMAGE
uks-control-plane       Ready    control-plane   8d    v1.29.0    10.0.1.243    Ubuntu 22.04.5 LTS
uks-docker-worker-17fd  Ready    <none>          8d    v1.29.0    10.0.2.49     Ubuntu 22.04.5 LTS
uks-docker-worker-2d69  Ready    <none>          8d    v1.29.0    10.0.1.197    Ubuntu 22.04.5 LTS
ip-10-0-2-64            Ready    <none>          42s   v1.29.15   10.0.2.64     Ubuntu 22.04.5 LTS
```

All 4 nodes showing Ready.

---

## 5. Installing Flannel (pod networking)

Without a CNI plugin, pods can't get IP addresses and they get stuck in ContainerCreating forever. I found this out the hard way — my first workload deployment just sat there and the describe output said "failed to load flannel subnet.env."

I installed Flannel on the control plane:

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
```

Then checked that all Flannel pods were running:

```bash
kubectl get pods -n kube-flannel -o wide
```

There should be one Flannel pod per node. If any of them were in CrashLoopBackOff, it meant that node was missing the br_netfilter kernel module — I had to SSH into that node and run the modprobe commands from section 4.

---

## 6. Labelling the Wasm worker node

The scheduler needs to know which node is the Wasm worker so it can route lightweight jobs there. I did this with a label:

```bash
kubectl label node ip-10-0-2-64 kubernetes.io/runtime-wasm=true
```

(The node name `ip-10-0-2-64` came from `kubectl get nodes` — the Wasm worker joined with its IP as its hostname instead of a custom name.)

I verified it with:

```bash
kubectl get nodes --show-labels | grep wasm
```

---

## 7. The Python code

The project code lives in `uncertainty-k8s-scheduler/` and I pushed it to GitHub at [github.com/nandhakumar12/uncertainty-k8s-scheduler](https://github.com/nandhakumar12/uncertainty-k8s-scheduler).

Here is how the files are organised:

```
uncertainty-k8s-scheduler/
├── scheduler_daemon.py      -- the real scheduler that talks to Kubernetes
├── run_experiments.py       -- runs the simulation benchmark (1,000 jobs)
├── plot_results.py          -- makes the results chart
├── requirements.txt         -- Python dependencies
├── Dockerfile               -- for packaging as a container
├── data/
│   ├── generate_trace_dataset.py  -- creates the trace CSV
│   └── google_trace_v3_sample.csv -- 5,000 tasks based on Google Cluster Trace v3
├── src/
│   ├── bnn_model.py         -- Bayesian Neural Network with MC Dropout
│   ├── data_loader.py       -- loads and parses the trace data
│   ├── schedulers.py        -- the 3 scheduler implementations
│   └── simulator.py         -- SimPy cluster simulator
└── k8s-manifests/
    ├── uks-scheduler/
    │   ├── rbac.yaml         -- permissions for the scheduler
    │   └── deployment.yaml   -- deploys the scheduler as a pod
    └── test-workloads/
        ├── docker-jobs.yaml  -- test Docker jobs
        └── wasm-jobs.yaml    -- test Wasm jobs
```

### How the BNN works (quick version)

The BNN is a small neural network (3 inputs, 64 hidden, 32 hidden, 1 output) written in pure NumPy. I didn't use PyTorch or TensorFlow because PyTorch had DLL issues on my Windows machine and I didn't want the extra dependency.

It trains on 3,000 rows from the Google Cluster Trace CSV. Training takes about 3 seconds. The clever part is Monte Carlo Dropout — during prediction it runs 30 forward passes with random dropout active each time. The average of those 30 predictions is the expected runtime (mu) and the spread is the uncertainty (sigma). A high sigma means the model isn't sure what will happen, which usually means it's a volatile Docker job.

### How the scheduler uses the BNN

The scheduler gets (mu, sigma) for each incoming job, then scores every node. The score formula is basically:

```
Score = (free memory on the node) - (penalty if the node can't handle a worst-case memory burst)
```

The burst is estimated as `3.5 × sigma`. If a node has enough free memory to absorb that burst, it gets no penalty. If it doesn't, the penalty makes the score drop significantly and the scheduler picks a different node.

---

## 8. Getting the code onto the cluster

On the control plane I cloned my repo and installed the Python dependencies:

```bash
cd ~
git clone https://github.com/nandhakumar12/uncertainty-k8s-scheduler.git
cd uncertainty-k8s-scheduler

sudo apt-get install -y python3-pip
pip3 install -r requirements.txt
```

The requirements are: simpy, numpy, pandas, matplotlib, scikit-learn, and kubernetes (the Python client).

---

## 9. Running the scheduler daemon

I ran the scheduler daemon directly on the control plane. It doesn't need Docker — it's just a Python script that talks to the Kubernetes API using the kubeconfig that's already on the node.

```bash
cd ~/uncertainty-k8s-scheduler
python3 scheduler_daemon.py
```

It printed:

```
[UKS] Loaded local kubeconfig
[UKS] Initialising BNN predictor from Google Cluster Trace data...
[UKS] BNN ready.
[UKS] UKS scheduler watching for pods with schedulerName=uks-scheduler
```

I left this running in one terminal tab.

---

## 10. Submitting real jobs

In a second terminal tab on the control plane, I created the namespace and applied the RBAC permissions:

```bash
kubectl create namespace uks-research
kubectl apply -f k8s-manifests/uks-scheduler/rbac.yaml
```

Then I submitted the test workloads:

```bash
kubectl apply -f k8s-manifests/test-workloads/docker-jobs.yaml
kubectl apply -f k8s-manifests/test-workloads/wasm-jobs.yaml
```

These jobs have `schedulerName: uks-scheduler` in their spec, so the default kube-scheduler ignores them and my custom daemon picks them up.

Switching back to the daemon terminal, I saw output like:

```
[UKS] New pod to schedule: uks-research/docker-job-high-mem-xxxxx
[UKS]   Node uks-docker-worker-17fd: free=12400MB mu=128.3 sigma=18.2 score=12271.7
[UKS]   Node uks-docker-worker-2d69: free=8200MB  mu=128.3 sigma=18.2 score=8071.7
[UKS] Decision: docker-job-high-mem-xxxxx → uks-docker-worker-17fd
[UKS] Bound pod docker-job-high-mem-xxxxx → uks-docker-worker-17fd (score-based)
```

That's the BNN calculating the memory uncertainty and the scheduler picking the node with the most headroom. Docker jobs went to the Docker workers, Wasm jobs went to the Wasm worker.

I verified pod placement with:

```bash
kubectl get pods -n uks-research -o wide
```

---

## 11. Running the benchmark simulation

The simulation is separate from the live cluster. It tests 1,000 jobs across 3 schedulers using SimPy. I ran it on my local machine:

```bash
cd uncertainty-k8s-scheduler
python run_experiments.py
```

Results:

```
Default Kube-Scheduler (LeastAllocated)    | 896 completed | 103 OOMKills | 10.40% fail rate
Deterministic ML Scheduler (Point mu)      | 896 completed | 103 OOMKills | 10.40% fail rate
Uncertainty-Aware Scheduler (Proposed)     | 928 completed |  72 OOMKills |  7.20% fail rate

Proposed scheduler reduced OOMKills by 30.1%
```

To generate the chart:

```bash
python plot_results.py
```

This saves `results/benchmark_results.png` at 300 DPI.

---

## 12. Problems I ran into and how I fixed them

| What went wrong | Why | What I did |
|----------------|-----|-----------|
| `kubeadm: command not found` on the Wasm worker | The user data script failed silently — Kubernetes packages never installed | Installed kubelet, kubeadm, kubectl manually (section 4) |
| Pods stuck in ContainerCreating | No CNI plugin installed | Installed Flannel (section 5) |
| Flannel pods crashing on Docker workers | br_netfilter kernel module wasn't loaded | Ran modprobe and sysctl commands on each worker |
| `SystemdCgroup` error during kubeadm join | containerd was using the wrong cgroup driver | Generated a new containerd config and set SystemdCgroup to true |
| Wasm worker joined with IP as hostname | Hostname wasn't set in the user data script | Used the IP-based name (`ip-10-0-2-64`) for all kubectl commands |
| docker.io install conflicted with containerd | Ubuntu sees them as conflicting packages | Skipped Docker entirely — ran the scheduler as a plain Python script |
| PyTorch DLL errors on Windows | Missing Visual C++ runtime | Rewrote the BNN in pure NumPy instead |
| AWS Academy blocked IAM role creation | Service Control Policy restriction | Used the pre-existing LabRole and LabInstanceProfile |
| SSH permission denied from CloudShell | Key file wasn't in CloudShell's filesystem | Used EC2 Instance Connect instead (no key needed) |
| Lab session expired mid-work | 4-hour timer | Went to Learner Lab and clicked Start Lab again |

---

## 13. Multi-cloud notes

My scheduler only talks to the standard Kubernetes API — it doesn't use any AWS-specific services for the actual scheduling decisions. This means it would work the same way on Azure VMs or GCP Compute Engine, as long as the worker nodes are running Ubuntu with Kubernetes and joined to a cluster.

For a multi-cloud setup I would create worker VMs on Azure or GCP, install kubeadm on them, and join them to the same control plane using the token from `kubeadm token create --print-join-command`. The scheduler would see them as additional nodes and score them the same way. This is documented as future work in my dissertation.

---

*Last updated: November 2026*
