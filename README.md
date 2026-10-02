# pve-ansible-compare

Compare the guests running on a **Proxmox VE** cluster (QEMU virtual machines
and LXC containers) with the hosts declared in the inventories of an
**Ansible** project.

The main use case is making sure that every guest on the cluster is declared in
the right Ansible inventory, according to the environment set in its Proxmox
tags.

## How it works

The Ansible project is expected to have this layout:

```
<ansible_project>/inventories/<location>/<env_name>.yml
```

- `<location>` usually corresponds to a single Proxmox cluster;
- `<env_name>.yml` is the inventory of one environment (`dev.yml`, `prod.yml`, ...).

On the Proxmox side, the environment of a guest is declared with a tag, by
default `env.<env_name>` (e.g. `env.dev`).

The script:

1. connects to the Proxmox API and reads all the cluster resources with a single
   call (`GET /cluster/resources`);
2. resolves the environment of every guest from its tags;
3. parses the inventories and looks the guest up in them;
4. prints a report with:
   - guests found in the inventory of their environment, with the groups they belong to;
   - guests found only in inventories of **other** environments;
   - guests not found in **any** inventory;
   - optionally, *orphan* hosts: declared in an inventory but not existing on the cluster;
   - guests that were excluded from the check (e.g. without environment tag), with the reason.

### Inventory parsing

Inventories are parsed following the Ansible YAML inventory structure:

- hosts can be declared at any level, under `hosts` of any group, nested in
  `children` at any depth;
- a group can be defined in one place and referenced with an empty body as a
  child of another group (e.g. `servers: {children: {webservers: }}`): membership is
  resolved globally, like Ansible does;
- a host can belong to several groups: the report lists all of them. Direct
  groups are printed as they are, inherited groups (parents of the direct ones)
  are printed in parentheses, e.g. `[webservers, (servers)]`;
- host ranges (`web[01:10].example.com`, `db-[a:c]`) and `host:port` entries
  are supported;
- Ansible specific YAML tags such as `!vault` and `!unsafe` are accepted (their
  values are not decrypted nor used);
- `group_vars`/`host_vars` directories, hidden files and editor backups
  (`*.yml~`) are ignored.

The environment of an inventory is its file name without extension; the
location is the name of its parent directory.

### Name matching

Proxmox guest names are often short names (`app1`) or partial FQDNs
(`app1.dev`) while inventories use full FQDNs (`app1.dev.site1.example.com`).
Names are compared case-insensitively, at three levels:

1. **exact**: same full name;
2. **prefix**: one name is the leading part of the other, label by label
   (`app1.dev` matches `app1.dev.site1.example.com`, not `app1.prod.site1.example.com`);
3. **short**: same first DNS label (`app1.lan` matches `app1.dev.site1.example.com`).

The `--match` option controls which levels are accepted:

| Mode     | Behaviour                                                                   |
|----------|-----------------------------------------------------------------------------|
| `auto`   | (default) all levels, with the refinements described below                  |
| `exact`  | exact level only                                                            |
| `prefix` | exact and prefix levels                                                     |
| `short`  | all levels, every match is kept                                             |

In `auto` mode, every guest and node of the cluster (excluded ones too) takes
part in the matching, and:

1. an inventory host belongs only to the cluster resources matching it at the
   strongest level. With the guests `app1.prod` and `app1.test` and the
   inventory hosts `app1.site1.example.com` (prod) and
   `app1.test.site1.example.com` (test), the latter matches `app1.test` at the
   prefix level, so it is not also given to `app1.prod`, which matches it at
   the short level only;
2. for each guest, only the strongest level among its remaining matches is
   kept: if `app1.dev` matches a host at the prefix level, short-name matches
   such as `app1.prod.site1.example.com` are ignored.

### Result categories

| Label  | Meaning                                                                                |
|--------|----------------------------------------------------------------------------------------|
| `OK`   | the guest is declared in an inventory of its environment                               |
| `WARN` | the guest is declared in an inventory of its environment, but **also** in inventories of other environments |
| `ENV`  | the guest is declared only in inventories of other environments                        |
| `MISS` | the guest is not declared in any inventory                                             |
| `ORPH` | (with `--orphans`) inventory host matching no guest nor node of the cluster            |
| `SKIP` | guest not checked: template, no environment tag, excluded by option, ...              |

A note is added when the environment of a guest has no inventory file at all,
or when a guest has more than one environment tag (in that case it is `OK` if
it is found in the inventory of any of them).

With `--match short` every name collision is kept, so guests sharing the first
DNS label (e.g. `web1.dev` and `web1.prod`) are reported as `WARN`; the default
`auto` mode avoids this.

## Requirements

- Python 3.9+
- [PyYAML](https://pypi.org/project/PyYAML/)
- [proxmoxer](https://pypi.org/project/proxmoxer/) and
  [requests](https://pypi.org/project/requests/) (not needed with `--resources-file`)

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Proxmox authentication

Either user/password or an API token can be used. The user needs at least the
`VM.Audit` and `Sys.Audit` privileges on `/` (the built-in `PVEAuditor` role is
enough). When using a token with privilege separation, assign the role to the
token itself.

```sh
# Create a read-only token (on a Proxmox node)
pveum user token add root@pam compare --privsep 1
pveum acl modify / --tokens 'root@pam!compare' --roles PVEAuditor
```

Connection parameters can also be given through environment variables, which
is preferable for secrets:

| Option          | Environment variable | Notes                                        |
|-----------------|----------------------|----------------------------------------------|
| `--host`        | `PVE_HOST`           | any node of the cluster                      |
| `--port`        | `PVE_PORT`           | default `8006`                               |
| `--user`        | `PVE_USER`           | with realm, e.g. `root@pam`; token owner too |
| `--password`    | `PVE_PASSWORD`       | prompted if neither password nor token given |
| `--token-name`  | `PVE_TOKEN_NAME`     | token ID, the part after `!`                 |
| `--token-value` | `PVE_TOKEN_VALUE`    | token secret                                 |
| `--insecure`    | `PVE_VERIFY_SSL=0`   | skip TLS certificate verification            |

## Usage

```sh
./pve_ansible_compare.py --host pve1.example.com --user root@pam \
    --token-name compare --token-value "$SECRET" \
    -i ~/ansible/project/inventories --location site1
```

### Options

**Proxmox connection**

| Option                  | Description                                                                 |
|-------------------------|-----------------------------------------------------------------------------|
| `--host`, `--port`      | Proxmox API endpoint                                                        |
| `--user`, `--password`  | user/password authentication                                                |
| `--token-name`, `--token-value` | API token authentication (with `--user` as token owner)             |
| `--insecure`            | do not verify the TLS certificate                                           |
| `--timeout SECONDS`     | API timeout (default 30)                                                    |
| `--resources-file FILE` | read resources from a JSON file instead of the API (see below)              |

**Ansible inventories**

| Option                 | Description                                                                  |
|------------------------|------------------------------------------------------------------------------|
| `-i`, `--inventory PATH` | inventories root (`<root>/<location>/<env>.yml`), a location directory or a single inventory file (required) |
| `-l`, `--location NAME`  | only use inventories of this location; repeatable. Recommended when the root contains inventories of several clusters (a warning is printed otherwise) |
| `--exclude-inventory GLOB` | skip inventory files matching the glob; repeatable. The pattern is checked against the file name (`client.yml`), the environment (`client`) and `<location>/<file>` (`site1/client.yml`). Only allowed when `-i` is a directory |

**Check options**

| Option                       | Description                                                            |
|------------------------------|------------------------------------------------------------------------|
| `--env-tag-format FMT`       | format of the environment tag, `{env}` is the placeholder (default `env.{env}`) |
| `--default-env ENV`          | environment for guests without tag; by default they are excluded and listed at the end |
| `--env-alias TAG=INV`        | map a tag environment to an inventory name, e.g. `production=prod`; repeatable |
| `--only-env ENV`             | only check guests of this environment; repeatable                       |
| `--include-nodes ENV`        | also check the Proxmox nodes, expected in the inventory of `ENV`. By default nodes are **not** checked |
| `--include-templates`        | also check templates (excluded by default)                              |
| `--skip-stopped`             | exclude guests that are not running                                     |
| `--exclude GLOB`             | exclude guests (and nodes) by name, e.g. `'test-*'`; repeatable          |
| `--exclude-tag TAG`          | exclude guests having this tag, e.g. `no-ansible`; repeatable            |
| `--match {auto,exact,prefix,short}` | name matching mode (default `auto`), see [Name matching](#name-matching) |
| `--orphans`                  | also report inventory hosts not existing on the cluster                 |
| `--orphans-exclude GLOB`     | ignore inventory hosts matching the glob in the orphan check (e.g. physical hosts, switches); repeatable |

**Output**

| Option                 | Description                                                       |
|------------------------|-------------------------------------------------------------------|
| `--format {text,json}` | output format (default `text`)                                    |
| `--hide-ok`            | do not list guests found in the right inventory (`WARN` guests are still listed) |
| `--hide-excluded`      | do not list excluded guests                                       |
| `--direct-groups-only` | do not show inherited groups                                      |
| `--no-color`           | disable colors (also disabled when `NO_COLOR` is set or output is not a terminal) |

### Exit codes

| Code | Meaning                                                                  |
|------|--------------------------------------------------------------------------|
| `0`  | every checked guest is `OK` (and no orphans, if requested)               |
| `1`  | discrepancies found: `WARN`, `ENV`, `MISS` or `ORPH` entries             |
| `2`  | error (connection, invalid inventory, invalid options)                   |

This makes the script usable in CI or monitoring checks.

### Examples

Check the `site1` cluster, showing only the problems:

```sh
export PVE_HOST=pve1.site1.example.com PVE_USER=root@pam
export PVE_TOKEN_NAME=compare PVE_TOKEN_VALUE=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
./pve_ansible_compare.py -i ~/ansible/project/inventories -l site1 --hide-ok
```

Also check the Proxmox nodes (expected in `hypervisors.yml`) and look for orphans,
ignoring network devices and printers:

```sh
./pve_ansible_compare.py -i ~/ansible/project/inventories -l site1 \
    --include-nodes hypervisors --orphans --orphans-exclude 'sw-*' --orphans-exclude 'printer-*'
```

Skip the inventories that do not describe cluster guests (e.g. desktop clients):

```sh
./pve_ansible_compare.py -i ~/ansible/project/inventories -l site1 --exclude-inventory client
```

Check a single inventory file, assigning `dev` to untagged guests:

```sh
./pve_ansible_compare.py -i ~/ansible/project/inventories/site1/dev.yml --only-env dev --default-env dev
```

Use a different tag format (e.g. `environment-prod`) and a JSON output:

```sh
./pve_ansible_compare.py -i inventories -l site2 --env-tag-format 'environment-{env}' --format json
```

### Offline mode

With `--resources-file` the cluster resources are read from a JSON file instead
of the API. This is handy to run the check without API credentials or to test
the script. Generate the file on any cluster node:

```sh
pvesh get /cluster/resources --output-format json > resources.json
./pve_ansible_compare.py -i inventories -l site1 --resources-file resources.json
```

## Sample output

```
Proxmox source: pve1.site1.example.com:8006
Inventories:    site1/dev.yml, site1/staging.yml

=== Guests found in the inventory of their environment (2) ===
  OK   db1.dev.site1.example.com (vmid 102, qemu, node pve1, running, env dev)
      site1/dev.yml: db1.dev.site1.example.com [databases, (servers)]
  OK   web1 (vmid 101, qemu, node pve1, running, env dev)
      site1/dev.yml: web1.dev.site1.example.com [webservers, (servers)]

=== Guests found in the inventory of their environment, but also in other environments (1) ===
  WARN mail1.site1 (vmid 106, qemu, node pve1, running, env staging)
      site1/staging.yml: mail1.site1.example.com [mail, (servers)]
      warning: also declared in inventories of other environments:
      site1/dev.yml: mail1.site1.example.com [mail]

=== Guests found only in inventories of other environments (1) ===
  ENV  app1 (vmid 103, lxc, node pve2, running, env prod)
      site1/dev.yml: app1.dev.site1.example.com [apps, (servers)]
      note: no inventory file for environment 'prod'

=== Guests not found in any inventory (1) ===
  MISS newvm (vmid 104, lxc, node pve2, stopped, env dev)

=== Excluded guests (not checked) (2) ===
  SKIP untagged (vmid 105, qemu, node pve2, running): no environment tag
  SKIP tpl-debian (vmid 9000, qemu, node pve2, stopped): template

=== Summary ===
  Checked guests:              5
  Found in right inventory:    2
  Also in other env (warning): 1
  Found in other env only:     1
  Not in any inventory:        1
  Excluded guests:             2
```
