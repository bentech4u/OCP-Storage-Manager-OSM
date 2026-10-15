# OCP Storage Manager (OSM)

A web console and command line tool for installing and operating the **Dell CSI PowerScale**
driver and **Dell CSM Replication** on OpenShift, including two-site failover.

Built with FastAPI and server-rendered pages. No build step, no content delivery network and no
database, so it runs happily on a disconnected installer host.

![Dashboard](docs/screenshots/home.png)

![Sign in](docs/screenshots/login.png)

## What it does

* Installs the PowerScale driver either with the **Dell CSM Operator** or with the **Helm chart**,
  standalone or with replication enabled.
* Connects to clusters three ways: sign in with a user such as kubeadmin, upload a kubeconfig, or
  point at one on the installer host. It can then create a service account with a non-expiring
  token and use that from then on.
* Registers PowerScale arrays, reading their version, access zones, licences, privileges and
  SyncIQ certificate, and creating the base path when it is missing.
* Builds the credentials secret, the plain storage class and mirrored replicated pairs, with the
  parameters and recovery point objectives the driver actually accepts.
* Wires two clusters together: replication controllers, `repctl` registration, configuration
  exchange and the peer config map.
* Runs failover, failback, reprotect, suspend, resume and sync against replication groups, and
  shows SyncIQ policies straight from the arrays.
* Tracks the recovery point per group: how long since the last successful sync, against the
  objective the driver set, read from the array rather than inferred. An idle volume is reported as
  idle rather than behind, because these policies run when the source changes.
* Runs pre-flight checks before any move between sites: whether an action is already running, the
  link state, the recovery point, the far cluster, its driver and replication controller, the far
  array and its SyncIQ licence, whether writers are still running at the source, and whether claims
  are waiting at the target. A move with failed checks is refused unless it is explicitly overridden,
  which is what a real outage at the other site looks like.
* Creates the claims at the site now serving a group, which a failover does not do by itself.
* Streams the output of every action into the page, and keeps the logs.
* Watches the lot on a timer and raises alerts by mail or webhook when something breaks: a cluster
  or array out of reach, a driver whose pods are not ready, a replication group in error, failed
  over and so unprotected, without a link state, or past twice its recovery point objective, a
  failed SyncIQ job, missing array privileges. Each is sent once when it starts, repeated only
  after a quiet period, and sent again when it clears.

Everything is also available from `osmctl`, so the console being down never blocks an install or
a failover.

## Pages

| Page | Purpose |
|---|---|
| Home | Cluster, array and driver health, replication group count, tool versions, recent activity |
| Operations | Replication groups and their actions, SyncIQ policies, job history with full logs |
| Install & Configure | Tools, clusters, arrays, driver install, storage classes, replication wiring |
| Setup | Administrator password, Microsoft Entra ID or other OpenID Connect sign-in, alerting, reset |

![Install and Configure](docs/screenshots/install.png)

![Operations](docs/screenshots/operations.png)

![Setup](docs/screenshots/setup.png)

## Requirements

* Linux host with Python 3.12, reachable from the OpenShift API endpoints and the array management
  addresses.
* `oc` or `kubectl` on the path. `helm` and `repctl` can be vendored in `bin/`, and the console can
  download or build `repctl` for you.
* Dell CSI PowerScale 2.17.1 with CSM Replication 1.15.0. Both charts are vendored in `charts/`.

## Install

```bash
git clone https://github.com/bentech4u/OCP-Storage-Manager-OSM.git /opt/ocpstorage
cd /opt/ocpstorage/app
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

useradd --system --home-dir /opt/ocpstorage --shell /bin/bash ocpstorage
chown -R ocpstorage:ocpstorage /opt/ocpstorage
cp deploy/ocpstorage-console.service /etc/systemd/system/
systemctl enable --now ocpstorage-console
ln -s /opt/ocpstorage/app/osmctl /usr/local/bin/osmctl
```

Open the console and choose an administrator password on first visit.

By default it listens on 8800 over plain http, which is fine on an installer host. To serve https
on 443 as an unprivileged service, make a certificate and give the unit the one capability that
allows binding a low port:

```bash
osmctl admin tls --host <the name or address people browse to>
```

```ini
[Service]
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Environment=OSM_PORT=443
Environment=OSM_TLS_CERT=/opt/ocpstorage/data/tls/console.crt
Environment=OSM_TLS_KEY=/opt/ocpstorage/data/tls/console.key
```

A self-signed certificate makes browsers warn; replace it with one from your own authority when
this is more than a lab.

## osmctl

```bash
osmctl status                       # tools, clusters, arrays, repctl store
osmctl doctor                       # health check of the whole installation
osmctl cluster add --id cluster-1 --server api.ocp.example.com --user kubeadmin
osmctl cluster serviceaccount --id cluster-1
osmctl array add --endpoint 10.0.0.10 --user csiuser --create-path --auth-type 1
osmctl driver install --cluster cluster-1 --mode replication --method operator
osmctl sc create --name isilon-replicated --cluster cluster-1 --array main \
    --replicated --target-cluster cluster-2 --target-array dr --rpo Five_Minutes
osmctl repctl setup --source cluster-1 --target cluster-2
osmctl rg list
osmctl rg failover --rg <group> --target cluster-2 --yes
osmctl rg preflight --rg <group> --source cluster-1 --target cluster-2
osmctl rg claims --rg <group> --namespace t24
osmctl alerts list | check [--send] | test
osmctl admin passwd                 # locked out of the console? set it here
osmctl admin backup --out /backup/osm.tar.gz
osmctl service restart | logs --follow
```

## Updating a site with no internet

The installer at a secured site cannot reach GitHub, so a version travels as one file.
Build it where you can pull, carry it across, apply it there.

On the machine with the repository:

```bash
osmctl update pack
```

That writes `dist/osm-<version>-<date>.tar.gz` from what git tracks, with a manifest
holding the commit and a sha256 for every file.

The machine that builds it needs nothing but git and python3: the packing code uses
only the standard library, so a clone is enough.

```bash
git clone https://github.com/bentech4u/OCP-Storage-Manager-OSM.git osm && cd osm
OSM_HOME=$PWD python3 app/osmctl update pack
```

On the installer:

```bash
osmctl update show osm-<version>-<date>.tar.gz   # what would change, before anything does
osmctl update apply osm-<version>-<date>.tar.gz
```

An installation older than these commands has to be given them once, out of the bundle
itself, before it can apply one:

```bash
tar -xzf osm-<version>-<date>.tar.gz -C /opt/ocpstorage \
    app/osmctl app/app/services/release.py app/app/config.py
```

`config.py` is in that list because a console older than the updater will not have
everything the updater reads from it. Extracting it costs nothing on a newer one: the
file is identical, and `apply` rewrites all three anyway, with the tree's ownership.
Only this first update needs the step at all.

Applying one:

- checks every file against its checksum and refuses a damaged bundle,
- keeps the current copy of everything it is about to write, under `data/backups`,
- writes the files, restarts the service and waits for `/healthz`,
- **puts the previous version back and restarts again if the console does not answer**,
- then re-reads the clusters, arrays and repctl store in a fresh process and prints what
  it found, so a bundle that breaks the integration is obvious straight away.

Nothing under `data`, `secrets`, `bin` or `.repctl` is in a bundle, so clusters, arrays,
kubeconfigs, array credentials and repctl's own store are never touched by an update.
Anything under `values/` that this site has changed is left alone and reported as kept.

`osmctl update rollback` puts the last backup back by hand. Both commands keep working
when the installed tree will not import, which is exactly when they are needed.

If `app/requirements.txt` changed in a bundle, the update says so: the virtual
environment is not touched, and on a host with no internet the new wheels have to be
carried across and installed with `.venv/bin/pip install -r app/requirements.txt`.

## Layout

```
app/        the console and osmctl, with its own virtual environment
bin/        helm and repctl
charts/     csi-isilon and csm-replication, vendored for offline installs
values/     base driver values
deploy/     systemd unit
docs/       screenshots
data/       state, kubeconfigs, job logs          (git-ignored)
secrets/    array credentials and certificates    (git-ignored)
.repctl/    repctl's own cluster store            (git-ignored)
```

## Exercised end to end

On a two-site lab of OpenShift 4.22 clusters and OneFS 9.15 simulators: driver installed by both
the CSM Operator and the Helm chart; volumes provisioned with quotas, snapshots and online
expansion; encrypted SyncIQ replication in both directions at once, one group each way; a failover
with the data verified at the far site, a reprotect, and a move back; sign-in with a local account
and with Microsoft Entra ID, including the multi-factor path through the provider's page.

## Notes from the field

These were checked against Dell's documentation and the driver source rather than assumed, and
they shape how the console behaves:

* The kubeconfig handed to `repctl` needs cluster-admin, because it installs custom resource
  definitions and cluster roles. The identity the replication controller uses at run time needs
  far less, only the `dell-replication-manager-role`, which is why the console offers service
  account tokens instead of injecting admin kubeconfigs.
* `repctl cluster inject --use-sa` only works after the replication controller exists, since it
  reads a token from a service account the chart creates.
* The credentials secret on **every** cluster must list **every** array, with exactly one default
  per cluster.
* `replicationCertificateID` is the SyncIQ target certificate, taken from the remote array's entry.
  On OneFS the identifier is the certificate's SHA-256 fingerprint.
* PowerScale does not implement `swap` or `establish`, so those actions are not offered.
* OneFS 9.15 ships with basic Platform API authentication disabled, which looks like a bare 401.
  It can be re-enabled with `isi_gconfig -t web-config auth_basic=true` followed by an apache2
  restart, and the console reports which authentication types each array accepts.
* A replicated storage class needs six parameters, not the three Dell's page calls mandatory, and
  the recovery point objective must be one of seven exact strings.

## Licence

Apache 2.0. The vendored Dell charts keep their own licences.
