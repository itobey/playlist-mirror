# Helm

A Helm chart is published for running Playlist Mirror on Kubernetes, maintained in
[itobey/charts](https://github.com/itobey/charts/tree/master/playlist-mirror).

```bash
helm pull oci://ghcr.io/itobey/charts/playlist-mirror --version 1.0.2
```

You can also find the chart on
[Artifact Hub](https://artifacthub.io/packages/helm/playlist-mirror/playlist-mirror), where
you can review its metadata, available versions, and installation details.

## Single-replica, single-user

The app keeps its job state in a local SQLite file under `DATA_DIR`. `replicaCount` must
stay at `1` — two pods would corrupt each other's writes — and `persistence.enabled` should
stay `true` (or point `persistence.existingClaim` at a volume you manage yourself). Without
persistence, the uploaded OAuth client secret, the token, and every job are lost on restart.

## Before first use

Set `config.publicBaseUrl` to wherever the app is actually reached, e.g.
`https://playlist.example.com`. Google sends the OAuth sign-in back to
`<publicBaseUrl>/oauth/callback`, so sign-in fails behind an ingress until this matches — the
app never derives it from the incoming request. See
[Installation & setup](/introduction/getting-started) for the full OAuth setup.

On first visit you land on `/setup` to upload a Google OAuth client secret JSON file and sign
in. In a cluster that has to be a **Web application** client with
`<publicBaseUrl>/oauth/callback` registered under **Authorized redirect URIs** — a "Desktop
app" client only ever accepts `http://localhost` and will fail with
`Error 400: redirect_uri_mismatch`. See
[Troubleshooting](/details/troubleshooting#error-400-redirect-uri-mismatch).

## Telegram

Telegram notifications are optional and off by default. The chart intentionally does not
accept a plaintext bot token or chat ID in `values.yaml` — set `telegram.secretRef.name` to
an existing secret with keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, or skip it and set
those up from the app's own Telegram page instead (stored in the database from then on). See
[Notifications](/details/notifications).

## Parameters

### Replica configuration

| Name | Description | Value |
| --- | --- | --- |
| `replicaCount` | Number of replicas to deploy. Must stay at 1: state lives in a local SQLite file, so two pods would corrupt each other's writes. | `1` |

### Image configuration

| Name | Description | Value |
| --- | --- | --- |
| `image.repository` | The image repository to pull from | `ghcr.io/itobey/playlist-mirror` |
| `image.tag` | The image tag to pull, defaults to the chart's appVersion | `""` |
| `image.pullPolicy` | The Kubernetes image pull policy | `IfNotPresent` |
| `imagePullSecrets` | Secrets used to pull the image from a private registry | `[]` |

### Persistence configuration

| Name | Description | Value |
| --- | --- | --- |
| `persistence.enabled` | Enable a persistent volume for DATA_DIR. Disabling it loses all state on restart. | `true` |
| `persistence.existingClaim` | Use an existing PVC instead of creating one. Leave empty to create one. | `""` |
| `persistence.storageClass` | Storage class for the created PVC. Leave empty to use the cluster default. | `""` |
| `persistence.accessMode` | Access mode for the created PVC. | `ReadWriteOnce` |
| `persistence.size` | Size of the created PVC. | `1Gi` |

### Application configuration

| Name | Description | Value |
| --- | --- | --- |
| `config.publicBaseUrl` | Where Google sends the user back after sign-in. Must be reachable at `/oauth/callback`. Required behind an ingress. | `""` |
| `config.playlistPrivacy` | Privacy of playlists this app creates. One of `private`, `unlisted`, `public`. | `private` |
| `config.quotaDailyLimit` | The project's daily YouTube Data API quota. Only seeds the figure — the app's stored value wins after that. | `10000` |
| `config.quotaReserve` | Quota units held back so a run never spends the last of the allocation on an insert it cannot confirm. | `50` |
| `config.quotaResetTz` | Timezone the daily quota resets in. | `America/Los_Angeles` |
| `config.schedulerInterval` | Seconds between checks of jobs marked "run this job daily, when credits allow". | `60` |
| `config.insertDelay` | Seconds paused between playlist inserts. | `0.2` |

### Telegram notification configuration

| Name | Description | Value |
| --- | --- | --- |
| `telegram.secretRef.name` | Existing secret name. Must have keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. | `""` |

### Ingress, service, probes and other standard fields

Ingress, service type/port, resources, node selector, tolerations, affinity, and liveness/
readiness probes follow the usual chart conventions — see the full
[values.yaml](https://github.com/itobey/charts/blob/master/playlist-mirror/values.yaml) for
every field. The app has no dedicated health endpoint; `/` redirects to `/setup` when not yet
connected, which still counts as a successful probe.
