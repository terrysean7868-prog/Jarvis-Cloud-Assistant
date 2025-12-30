# AI Assistant

A general-purpose AI assistant application designed to help users interact through natural language.

This README intentionally avoids implementation details (such as tech stack, internal structure, or deployment specifics). It focuses only on the product-level purpose and safe usage.

## What it does

- Conversational assistance for everyday questions and tasks
- Optional voice interaction (depending on the client/device capabilities)
- User accounts and personalization (when enabled)
- Optional connection to a companion client for performing actions on a user-owned machine (when configured)

## Safety and privacy

- Review your configuration before enabling features that can access external services or perform actions on a device.
- Treat any credentials/tokens as sensitive and store them using environment variables or your hosting provider’s secret manager.
- If you expose the assistant to the public internet, ensure authentication is enabled and restrict any high-risk capabilities.

## Setup

Setup steps are documented separately to keep this README generic.

- Installation and configuration: see `docs/INSTALL.md`
- Feature notes and guides: see the `docs/` folder

## Support

If you run into issues, capture:

- What you were trying to do
- The exact error message
- Relevant logs (with secrets removed)
