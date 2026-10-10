# Portable AWS company container

The AWS node uses the same pinned `deploy/compose/company-chatmode.yml` and
`cognilode-company-container-bootstrap` source as EvanPC. The one-command
entrypoint is `scripts/cognilode-aws-company-bootstrap --activate`. Without
`--activate`, it emits a machine-readable readiness receipt and changes nothing.
It fails closed before Compose when the instance, capacity, Docker daemon,
preloaded workspace image, operator enrollment, or scoped STS role is absent.
The central broker route must already serve a scoped session; this command
enrolls an additional AWS company container and does not bootstrap the first
credential-bearing broker from an empty central state.

The bootstrap sets `COGNILODE_PORTABLE_AWS=1`. Existing Compose clients then
mount the `company_aws_client` named volume with `credential_process`; the
central `/api/operator/aws-session` route obtains short-lived sessions from the
registered AWS broker container. No host `~/.aws`, browser profile, static key,
or personal EvanPC state is copied to the AWS node. The role ARN is derived
from the instance account ID, while source-sync and service configuration come
from the same pinned SDK checkout. The image must be supplied as the existing
versioned company image; this flow never builds an image.

The IAM provisioning artifact is
`scripts/cognilode-provision-aws-broker-role`. An authorized company IAM
environment runs it once with
`COGNILODE_AWS_BROKER_TRUST_PRINCIPAL` set to the node's instance-profile role
ARN. It idempotently creates the resource-scoped broker role, sets trust for
that one role, and grants the instance role `sts:AssumeRole` only for the
scoped broker. Purpose-specific STS session policies further narrow the
broker's credentials. The AWS node itself does not need IAM write access.

The existing `aws-public-relay-20261002` is not ready for this cutover. On
October 10, 2026, IMDSv2 identified us-west-2 t3.nano instance
`i-0d12e1fe0683cbc43` with role
`arn:aws:iam::362928919715:role/cognilode-public-relay-bg-20261002`.
Its root filesystem had zero free bytes, available RAM was about 175 MiB,
and Docker was absent. The instance role was denied `iam:GetRole` and
`sts:AssumeRole` for `CognilodeCompanyScopedBroker`; the current company
source-sync AWS CLI also failed before an administrative IAM call. Thus the
script and scoped role policy are reviewable, but neither the role nor the AWS
container has been activated. Provide capacity, the versioned image and
operator enrollment, an authorized IAM provision route, and an already live
central broker route before running `--activate` there. The initial broker
container/bootstrap path remains blocked on those physical and IAM conditions.
The EvanPC container continues to use its existing
credential path until the broker returns a scoped live session.
