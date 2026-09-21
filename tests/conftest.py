"""Settings every test shares.

The model runs from recorded replies. When one is missing the provider falls back to
the live service, and the AWS SDK first looks for credentials at the cloud instance
metadata address - which, off a cloud instance, waits on the network until it times
out. Tests must not need credentials or a network, so that lookup is switched off.
"""
import os

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
