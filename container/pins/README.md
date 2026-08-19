# Build-input pins

An image rebuilt a year from now must install the *same* package versions, or a campaign
re-run from its records runs different software while reporting the same provenance. Pinning
versions alone does not achieve that: `apt-get install pkg=1.2.3-4` fails once the archive
drops the superseded version, and both `packages.ros.org` and the Ubuntu archive keep only
current ones. What makes an old version *available* is a point-in-time archive.

Everything here was verified against the live services, not assumed. `snapshot-probe.Dockerfile`
is that verification, kept so the recipe can be re-checked in one `docker build` when a step
stops working.

## The two archives, and how they differ

| | endpoint | granularity |
|---|---|---|
| Ubuntu | `https://snapshot.ubuntu.com/ubuntu/<YYYYMMDDTHHMMSSZ>` | any timestamp since 2023-03-01 |
| ROS | `http://snapshots.ros.org/<distro>/<YYYY-MM-DD>/ubuntu` | a **fixed set** of dates, roughly quarterly |

That asymmetry matters for tooling: an Ubuntu pin can be computed from any date, while a ROS pin
has to be **discovered** from what the server actually offers. Asking for an arbitrary ROS date
gets a 404, not the nearest snapshot.

## Four things that only showed up by running it

1. **Install `ca-certificates` from the default archive first.** `snapshot.ubuntu.com` redirects
   HTTP to HTTPS, so apt cannot reach it until the trust store exists — and it cannot install the
   trust store from a source it cannot reach. Switching sources before that step fails with
   "certificate is NOT trusted", which reads like a broken snapshot rather than an ordering bug.
2. **The ROS snapshots are signed by a different key than the live repo.**
   `AD19BAB3CBF125EA`, *ROS Snapshot builder \<rosbuild@ros.org\>*. The current `ros.key` does not
   verify them: `apt-get update` fails with `NO_PUBKEY AD19BAB3CBF125EA`.
3. **Fetch that key with `curl`, not `gpg --recv-keys`.** The keyserver protocol needs `dirmngr`
   and outbound access a build container may not have; `gpg --recv-keys` exits 2 there. The HTTPS
   keyserver interface plus `gpg --dearmor` needs neither.
4. **`--dearmor` is required.** `ros.key` and keyserver output are ASCII-armored, while a
   `signed-by=…/x.gpg` source expects a binary keyring, and the mismatch reports as `NO_PUBKEY`
   — the same symptom as the wrong key, which makes it easy to misdiagnose.

## Verified output

```
ubuntu-snapshot Candidate: 2.1.1-2ubuntu3.24.04.2
ros-snapshot    Candidate: 0.11.0-1noble.20260615.174419
```

The ROS version string carries the snapshot's own build stamp (`20260615.174419`), which is what
makes a pinned rebuild checkable: the version says which snapshot produced it.

## Re-verifying

```
docker build -f container/pins/snapshot-probe.Dockerfile \
  --build-arg UBUNTU_SNAPSHOT=<stamp> --build-arg ROS_SNAPSHOT=<date> container/pins
```

Cheap (a small base image, two `apt-get update`s) and it fails loudly on any of the four points
above, so it is the thing to run before blaming a rebuild.
