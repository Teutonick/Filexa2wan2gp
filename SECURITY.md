# Security Policy

## Scope

This security policy applies only to the Filexa2Wan2GP Connector code published in this repository.

It does not apply to:

- Wan2GP itself;
- third-party AI models, checkpoints, VAEs, text encoders, or other model files;
- GPU drivers, CUDA, Python, operating systems, or other runtime components;
- Telegram, hosting providers, payment providers, or other external services;
- the user's local computer, local network, or Wan2GP installation;
- the Filexa hosted service, unless the issue is directly caused by this connector.

## Supported Versions

Security fixes are provided only for the latest public version of the connector.

Older versions may not receive security updates. Users should update to the latest available release before reporting an issue.

## Reporting a Vulnerability

If you believe you have found a security vulnerability in the connector, please report it privately.

Contact:

**thismailnotbad@gmail.com**

If a dedicated security contact is later published in the repository README or in the Filexa bot, use that contact instead.

Please include as much detail as possible:

- affected connector version or commit;
- operating system;
- Wan2GP version, if relevant;
- steps to reproduce;
- expected behavior;
- actual behavior;
- logs, screenshots, or proof-of-concept details, if safe to share;
- whether the issue may expose tokens, prompts, generated files, local files, or service data.

Please do not publicly disclose the vulnerability until it has been reviewed.

## What Counts as a Security Issue

Examples of issues that should be reported:

- Filexa API token leakage;
- insecure storage of credentials;
- unintended access to local files;
- unintended exposure of the local Wan2GP API to the internet;
- command execution or code execution vulnerabilities;
- path traversal;
- unauthorized task execution;
- ability for one user to access another user's tasks or results;
- unsafe handling of generated files or temporary files;
- transmission of sensitive data to an unexpected endpoint;
- dependency vulnerabilities that are directly exploitable through the connector.

## What Is Usually Not a Connector Security Issue

The following are usually outside the connector's scope:

- poor output quality from an AI model;
- unsafe, biased, infringing, or unwanted model outputs;
- high GPU, CPU, RAM, or VRAM usage during generation;
- overheating, fan noise, power usage, or hardware wear caused by local generation;
- problems caused by manually modified Wan2GP installations;
- vulnerabilities in Wan2GP itself;
- vulnerabilities in third-party models or model download sources;
- issues caused by exposing Wan2GP or other local services to the internet;
- account, billing, or policy issues related to Filexa service usage.

These may still be useful bug reports, but they are not necessarily security vulnerabilities in this repository.

## Responsible Testing

When testing the connector, do not:

- attack, scan, overload, or disrupt Filexa infrastructure;
- attempt to access other users' accounts, tokens, tasks, prompts, files, or generated results;
- publish real tokens, private URLs, logs, prompts, images, or personal data;
- use social engineering, phishing, malware, or persistence techniques;
- test against systems you do not own or have permission to use;
- violate Wan2GP, Telegram, model provider, hosting provider, or other third-party terms.

Security research should be limited to your own account, your own connector installation, and test data you are allowed to use.

## Token and Credential Safety

Users are responsible for keeping their Filexa API token private.

Do not publish tokens in:

- GitHub issues;
- pull requests;
- screenshots;
- logs;
- public chat messages;
- support requests;
- videos or tutorials.

If a token may have been exposed, revoke or rotate it as soon as possible using the Filexa bot or service interface.

The connector should be configured only with Filexa API URLs and tokens obtained from the official Filexa bot or service.

## Network Behavior

The connector is designed to make outbound HTTP/HTTPS requests to the configured Filexa API endpoint.

It does not require exposing the user's local Wan2GP port to the public internet.

Users should not open public inbound access to Wan2GP unless they understand the security risks and have configured appropriate authentication, firewalling, and network controls.

## Dependency Security

This project depends on third-party libraries supplied by the user's local Wan2GP installation and Python environment.

Users and contributors should keep dependencies up to date and review dependency changes before use.

A vulnerability in a third-party dependency may be outside this repository's control, but reports are welcome if the vulnerability is directly exploitable through the connector.

## Disclosure Process

After receiving a vulnerability report, the maintainers will review the issue and may request additional information.

If the issue is confirmed, a fix may be prepared and released in a new version.

Public disclosure should happen only after a fix is available or after the maintainers have had a reasonable opportunity to investigate the issue.

## No Bug Bounty

This project does not currently offer a paid bug bounty program.

Reports are welcome, but submitting a report does not create any right to compensation, reward, employment, service credit, or other benefit.

## Disclaimer

This connector is provided as open-source software under the MIT License and is provided "as is", without warranty of any kind.

Users install, configure, and run the connector, Wan2GP, third-party models, and local generation software at their own risk.
