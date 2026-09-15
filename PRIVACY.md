# Privacy Policy

**Effective date:** 15 September 2026

`photos-shrink` ("the software") is an open-source command line tool that runs
entirely on the computer of the person using it. It re-encodes photos and videos
to smaller files and, with that person's explicit approval, replaces the
originals in their own Google Photos library.

There is no `photos-shrink` service, server, or hosted component. The authors of
the software cannot see, receive, or access any data belonging to anyone who
runs it.

## What the software collects

**Nothing.** The software has no analytics, no telemetry, no crash reporting,
no usage statistics, and no "phone home" of any kind. It transmits no
information to the authors or to any third party.

The only network destinations it contacts are Google's own services, acting on
behalf of the account holder who authorized it.

## Google account data

When authorized, the software requests these Google Photos scopes:

| Scope | Why it is needed |
| --- | --- |
| `photoslibrary.appendonly` | To upload the smaller re-encoded files into the account holder's own library. |
| `photoslibrary.readonly.appcreateddata` | To read back the items it just uploaded, so it can confirm they stored correctly before anything is removed. |

The second scope grants access **only to items the software itself created**. It
cannot read the rest of the library.

Data obtained through these scopes is used solely to perform the re-encoding and
replacement the account holder asked for. It is never sold, shared, transferred,
or used for advertising, profiling, training machine learning models, or any
purpose unrelated to that task.

Use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Where data is stored

Everything stays on the account holder's own computer, by default under a
`.photos-shrink` directory beside the configuration file:

- **OAuth refresh token** (`api-token.json`) — grants upload access to the
  authorized account. Stored locally only; never transmitted anywhere except to
  Google when exchanging it for an access token.
- **OAuth client credentials** (`api-client.env`) — created by the account
  holder in their own Google Cloud project.
- **Exported browser cookies**, if the optional browser-based features are used.
- **Copies of the account holder's own photos and videos**, both originals and
  re-encoded versions, kept so that an interrupted run can resume and so that
  originals are recoverable.
- **A local database** recording which items were processed.

None of these files leave the computer. All of them are excluded from the
software's source repository.

Because these files grant access to a Google account and contain personal
media, whoever runs the software is responsible for protecting them and for
deleting them when finished.

## Removing access

Access can be revoked at any time, independently of the software, at
[Google Account permissions](https://myaccount.google.com/permissions). Deleting
the local `.photos-shrink` directory removes every stored credential and every
cached copy of media from the computer.

Revoking access does not delete anything already uploaded to Google Photos.
Items in Google Photos are managed through Google Photos itself.

## Children

The software is a developer tool and is not directed at children.

## Changes

Changes to this policy are made by committing to this file; its history is
public in the repository.

## Contact

Questions about this policy can be raised as an issue at
<https://github.com/johnml1135/google-photos-shrink/issues>.
