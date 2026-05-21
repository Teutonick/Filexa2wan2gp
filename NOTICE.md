# Notices and Disclaimers

This repository contains the Filexa2Wan2GP Connector, an open-source connector
that allows a user's local Wan2GP installation to receive generation tasks from
Filexa and return supported image/video results or local-only completion status to Filexa.

Not affiliated with, endorsed by, or sponsored by Wan2GP.

The connector stores its local configuration, including the Filexa API token,
inside the user's local Wan2GP plugin directory.

## Separate License and Service Terms

The source code in this repository is licensed under the MIT License.

The Filexa bot, Filexa API, hosted backend, accounts, tokens, payments, usage
limits, acceptable use rules, privacy terms, and service availability are not
licensed under the MIT License. They are provided, if at all, under the separate
Filexa Terms of Use and Privacy Policy:

https://teutonick.github.io/bot-legal-docs/privacy

Using this connector does not grant any right to access the Filexa service
except as permitted by the applicable Filexa service terms.

## Third-Party Software

This connector is intended to work with Wan2GP, but it is not part of Wan2GP
and is not endorsed by, sponsored by, or affiliated with the Wan2GP project
unless explicitly stated by that project.

Wan2GP is third-party software. Users install, configure, update, and operate
Wan2GP at their own risk and subject to Wan2GP's own license, documentation,
and terms.

This repository does not include Wan2GP, AI models, model weights, checkpoints,
text encoders, VAEs, CUDA, GPU drivers, or any other third-party runtime
components unless explicitly stated.

## Third-Party Models and Model Licenses

Users are solely responsible for selecting, downloading, installing, and using
any AI models or model weights.

AI models may have their own licenses, restrictions, commercial-use limitations,
acceptable-use rules, attribution requirements, or other legal conditions. The
MIT License for this connector does not apply to third-party models and does not
grant any rights to use third-party models.

Before using any model, especially for commercial purposes, users must review
and comply with the model provider's license and terms.

## Local Execution and Network Behavior

The connector runs inside the user's local Wan2GP installation.

The connector is designed to make outbound HTTP/HTTPS requests to the configured
Filexa API endpoint. It does not require exposing the user's local Wan2GP port
to the public internet.

The connector does not give the developer remote desktop access, shell access,
file-system access, or administrative access to the user's computer.

The connector can cause local generation tasks to run on the user's machine
when enabled and connected to Filexa. Users are responsible for reviewing their
configuration, model selection, resource usage, and local security before
enabling the connector.

## Data and Privacy

The connector may transmit task data, prompts, generation parameters, status
information, errors, and generated results between the user's local Wan2GP
installation and Filexa, depending on how the connector and Filexa service are
configured.

Users should not submit personal data, confidential data, sensitive data,
third-party copyrighted materials, unlawful content, or content they are not
allowed to process unless they have the necessary rights and legal basis to do
so.

The Filexa service's collection, processing, retention, and deletion of data are
governed by the applicable Filexa Terms of Use and Privacy Policy, not by the
MIT License.

## No Warranty

The connector is provided "as is" and "as available", without warranties of any
kind, whether express, implied, statutory, or otherwise.

The authors and copyright holders do not warrant that the connector will be
secure, uninterrupted, error-free, compatible with any specific Wan2GP version,
compatible with any specific model, suitable for any particular purpose, or free
from harmful components.

## No Responsibility for Third-Party Components

The authors and copyright holders are not responsible for Wan2GP, AI models,
model outputs, model licenses, third-party dependencies, GPU drivers, operating
system behavior, Telegram, hosting providers, payment providers, or any other
third-party software, service, or infrastructure.

## User Responsibility

Users are solely responsible for:

- installing and operating Wan2GP;
- downloading and using models lawfully;
- verifying third-party licenses;
- securing their local computer and API tokens;
- ensuring adequate hardware, cooling, power, and storage;
- reviewing generated outputs before use;
- complying with applicable laws and third-party terms;
- backing up important data;
- disabling or removing the connector if they no longer want it to run.

## Limitation of Liability

To the maximum extent permitted by applicable law, the authors and copyright
holders shall not be liable for any direct, indirect, incidental, special,
consequential, exemplary, punitive, or other damages, losses, costs, or claims
arising from or related to the connector, Filexa service integration, Wan2GP,
third-party models, generated outputs, local configuration, hardware usage,
data loss, security incidents, service interruption, or third-party license
violations.

Some jurisdictions do not allow certain warranty exclusions or liability
limitations. In those jurisdictions, the exclusions and limitations apply only
to the maximum extent permitted by law.

## No Legal, Professional, or Compliance Advice

The connector and its documentation do not provide legal, compliance, security,
medical, financial, or other professional advice. Users are responsible for
obtaining appropriate professional advice where needed.

## Trademarks

Filexa and related names, logos, and branding are trademarks or identifiers of
their respective owners.

Wan2GP and other third-party names, logos, and trademarks belong to their
respective owners. Their mention in this repository is for compatibility and
identification purposes only and does not imply endorsement, sponsorship, or
affiliation.
