# OCP Storage Manager

A small web console for installing and operating the Dell CSI PowerScale driver and CSM
Replication across two sites. FastAPI on the server, server rendered pages with HTMX for
live updates, no build step and no content delivery network.

```bash
./run.sh                      # http://<host>:8800
OSM_PORT=9000 ./run.sh     # another port
```

On first start the console asks for an administrator password and stores a bcrypt hash of it.

## Pages

| Page | What it does |
|---|---|
| Home | Cluster, array, driver and replication group health, tool versions, recent jobs |
| Operations | Replication groups with failover, failback, reprotect, suspend, resume and sync; SyncIQ policies straight from the arrays; job history with full logs |
| Install and Configure | repctl, clusters, arrays, driver install by Operator or Helm, storage classes, replication wiring |
| Setup | Administrator password, Microsoft Entra ID or other OpenID Connect provider, alerting |

Every long action runs as a background job whose output streams into the page and is also
written to `data/logs/`.

## Install and Configure, step by step

1. **Tools.** Install repctl by downloading Dell's published binary, uploading one from a
   disconnected host, or building it in a container. Releases v1.14.0 and v1.15.0 publish no
   binary, so the release list marks which tags can be downloaded. Whatever route you pick, the
   binary lands in `data/bin/repctl` and the Tools panel shows that path.
2. **Clusters.** Add each cluster one of three ways:
   * **Sign in with a user**, which is the kubeadmin route. Give the API address, a user with
     administrator rights and the password. The console runs the login, keeps the resulting
     token and never stores the password.
   * **Upload a kubeconfig** from your workstation.
   * **Path on this host** for a kubeconfig that already exists on the installer.

   *Check access* reports whether the identity may create custom resource definitions, cluster
   roles, secrets and the rest. *Export service account* creates a service account with a
   non-expiring token, binds it to cluster-admin and writes a kubeconfig the console uses from
   then on, so nothing later depends on a session that expires.
3. **Arrays.** Register each PowerScale array. Choose how the Platform API is reached, session
   (`isiAuthType: 1`) or basic (`isiAuthType: 0`); OneFS 9.15 and later accept session only, and
   the console says so plainly if basic is refused. It then reads the array's version, zones,
   licences, privileges and SyncIQ certificate, and can create the base path. The driver install
   follows the array's setting unless you override it.
4. **Driver.** Choose Operator or Helm, and standalone or replication. The credentials secret
   always lists every registered array, because the driver resolves the far end of a SyncIQ pair
   from its own configuration.
5. **Storage classes.** A plain class, or a mirrored pair created on both clusters at once.
6. **Replication.** Installs the replication controller on both sites, registers them with
   repctl and exchanges their configurations.

## What was verified against Dell's documentation and source

* **The kubeconfig given to repctl does need cluster-admin.** repctl installs custom resource
  definitions, cluster roles and cluster role bindings on both clusters, and creating those
  requires cluster-admin or an explicit escalate and bind grant. Dell's pages say "cluster admin
  configurations" without naming a role, and repctl never checks; the access check page lists
  each permission so a narrower role can be proven before use.
* **The identity the replication controller uses at run time does not.** Dell's documented
  minimum is the `dell-replication-manager-role` cluster role, and Dell recommends the
  `dell-replication-controller-sa` service account token. That is why the console offers service
  account tokens rather than injecting admin kubeconfigs, which Dell itself labels less secure.
* **repctl does not create service accounts.** It copies each kubeconfig into its own store,
  and with `--use-sa` it reads a token from a service account that the controller chart already
  created. So the controller must be installed before that step, which is the order this console
  uses.
* **Both arrays must appear in the credentials secret on both clusters**, with exactly one
  default entry per cluster.
* **`replicationCertificateID` is the SyncIQ target certificate**, read from the remote array's
  entry. On OneFS the certificate identifier is the SHA-256 fingerprint.
* **PowerScale rejects two repctl actions.** Swap and establish are not implemented by this
  driver, so the console does not offer them.
* **Recovery point objective values are a fixed list** of seven, and a replicated storage class
  really needs six parameters, not the three Dell's page calls mandatory. The console validates
  both.
* **Versions in this lab sit outside Dell's support matrix.** CSM 1.17 lists OneFS up to 9.14
  and OpenShift up to 4.21, while the lab runs OneFS 9.15 and OpenShift 4.22. Installs note this
  rather than blocking.

## Layout

```
app/main.py            application, session handling, login
app/config.py          paths and defaults
app/store.py           JSON state file
app/security.py        password hashing and signed session cookies
app/services/tools.py        detect and install repctl, helm, oc
app/services/k8s.py          cluster probing, access checks, service account kubeconfigs
app/services/arrays.py       OneFS Platform API client and secret builder
app/services/installer.py    Helm and Operator installs, storage classes
app/services/replication.py  repctl wrappers and preflight checks
app/services/inventory.py    cached snapshot for the dashboard
app/services/jobs.py         background jobs with streamed output
data/                  state, kubeconfigs, repctl home, logs, installed binaries
```

## The command line, when the console is down

Everything the pages do is also a command, against the same state, so nothing depends on the
web interface being up. `osmctl` is linked into `/usr/local/bin`.

```bash
osmctl status                                   # tools, clusters, arrays, repctl store
osmctl cluster add --id cluster-2 --server api.dr.example.com --user kubeadmin
osmctl cluster serviceaccount --id cluster-2    # non-expiring token kubeconfig
osmctl cluster rbac --id cluster-2
osmctl array add --endpoint 192.168.68.40 --user csiuser --create-path
osmctl driver install --cluster cluster-2 --mode replication --method helm
osmctl sc create --name isilon-replicated --cluster cluster-1 --array isin-main \
    --replicated --target-cluster cluster-2 --target-array isin-dr --rpo Five_Minutes
osmctl repctl register                          # repctl cluster add for every known cluster
osmctl repctl setup --source cluster-1 --target cluster-2
osmctl repctl run cluster get                   # anything else, straight through to repctl
osmctl rg list
osmctl rg failover --rg <group> --target cluster-2 [--unplanned] [--yes]
```

Passwords can come from `--password`, from the `OSM_PASSWORD` and `OSM_ARRAY_PASSWORD`
variables, or from a prompt, so they need not appear in shell history.

### Running the console itself

```bash
osmctl doctor                       # tools, paths, permissions, clusters, arrays, repctl
osmctl admin show                   # paths and settings at a glance
osmctl admin passwd                 # set or reset the console password, prompts by default
osmctl admin forget-password        # next visit runs first-time setup again
osmctl admin revoke-sessions        # rotate the signing key, signing everyone out
osmctl admin backup --out /backup/console.tar.gz
osmctl admin restore --file /backup/console.tar.gz --force
osmctl admin reset --scope inventory|everything
osmctl service start|stop|restart|status|logs [--follow]
osmctl jobs [--limit 20] [--show <job id>]
```

`osmctl admin passwd` is the way back in when nobody can sign in. The backup covers the state
file, the kubeconfigs, the repctl store and the secrets directory, which is everything that
cannot be rebuilt by reinstalling.

Run `osmctl` as root or as the service user; anything it writes is handed back to the user that
owns the data directory, so the console keeps working either way.

## repctl state

repctl reads `$HOME/.repctl/clusters` and offers no way to move it, so the console uses exactly
that path. Clusters registered in the web pages are visible to `repctl` run by hand, and the
other way round. Installing repctl also links it into `/usr/local/bin`, so plain `repctl cluster
get` works from any shell.

## Starting over

Setup has a **Start over** panel. Clearing the inventory removes clusters, arrays, stored
kubeconfigs and repctl state while keeping your password and settings; clearing everything also
removes the password, so the next visit begins at first-run setup. Neither option uninstalls
anything from a cluster or an array.

## Data and secrets

`data/` holds array passwords, kubeconfigs and service account tokens with owner-only
permissions. It is excluded from version control. Put the console behind a reverse proxy with
TLS before exposing it beyond the installer host.
