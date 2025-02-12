#!/usr/bin/python3

# Copyright 2019 Adobe. All rights reserved.
# This file is licensed to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR REPRESENTATIONS
# OF ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""
This script is a replacement for the original Python 2 scripts for directly querying a CT log.
If you have never run it before, then it will likely take a long time to process since a CT log
typically has millions of certificates. Afterwards, it will only search new certificates added
to the log since the last run which will dramatically decrease the run time.

The script will create jobs in the job table based on which CT Log it is run against. The format
will be "ct_log-{source}".

The known values for the log source are defined in the libs3/X509Parser library.

Whether the certificates are saved to disk is optional. This script will create the directory for
each ct log in the specified save_location under ct_{source}.

With certificate transparency logs, there are pre-certificate and certificate entries. A CA can
submit to the CT-Log a pre-certificate entry, a certificate-entry, either one, or both. A
pre-certificate entry is not a fully valid certificate. It is merely a record of the intent to
create a certificate. While the CA will likely go on to create the final certificate based on
the submitted pre-certificate, they may or may not submit the final certificate back to the log.
The pre-certificate and the final certificate would not have the same SHA fingerprint since the
pre-certificate is not the final version of the certificate. This script provides the option of
recording the pre-certificate entries in the database assuming that a valid certificate was
eventually generated. Since the pre-certificate and certificate will have different hashes,
it would require extra work on your part to match a pre-certificate to a certificate. These
scripts will indicate whether the recorded certificate is a pre-certificate or certificate
in the 'ct_log_type'.
"""

import argparse
import base64
import json
import logging
import os
import struct
import time
from datetime import datetime

import requests
from OpenSSL import crypto
from requests.adapters import HTTPAdapter
from requests.exceptions import Timeout
from urllib3.util import Retry

from libs3 import JobsManager, MongoConnector, X509Parser
from libs3.LoggingUtil import LoggingUtil
from libs3.ZoneManager import ZoneManager


def requests_retry_session(
    retries=5,
    backoff_factor=7,
    status_forcelist=[408, 500, 502, 503, 504],
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def make_https_request(logger, url, jobs_manager, download=False, timeout_attempt=0):
    """
    Utility function for making HTTPs requests.
    """
    try:
        req = requests_retry_session().get(url, timeout=120)
        req.raise_for_status()
    except requests.exceptions.ConnectionError as c_err:
        logger.error("Connection Error while fetching the cert list")
        logger.error(str(c_err))
        jobs_manager.record_job_error()
        exit(1)
    except requests.exceptions.HTTPError as h_err:
        logger.warning("HTTP Error while fetching the cert list")
        logger.warning(str(h_err))
        return None
    except requests.exceptions.RequestException as err:
        logger.error("Request exception while fetching the cert list")
        logger.error(str(err))
        jobs_manager.record_job_error()
        exit(1)
    except Timeout:
        if timeout_attempt == 0:
            logger.warning("Timeout occurred. Attempting again...")
            result = make_https_request(
                logger, url, jobs_manager, download, timeout_attempt=1
            )
            return result
        else:
            logger.error("Too many timeouts. Exiting")
            jobs_manager.record_job_error()
            exit(1)
    except Exception as e:
        logger.error("UNKNOWN ERROR with the HTTP Request: " + str(e))
        jobs_manager.record_job_error()
        exit(1)

    if req.status_code != 200:
        logger.error("ERROR: Status code " + str(req.status_code))
        return None

    if download:
        return req.content

    return req.text


def fetch_sth(logger, url, jobs_manager):
    """
    Fetch the initial STH record from the CT Log.
    """
    result = make_https_request(logger, url + "/ct/v1/get-sth", jobs_manager)
    return json.loads(result)


def fetch_certificate_batch(logger, url, starting_index, ending_index, jobs_manager):
    """
    Fetch a range of records from the CT Log.
    Each log has its own limits on the number of records returned which means
    that the ending_index may be ignored.
    """
    result = make_https_request(
        logger,
        url
        + "/ct/v1/get-entries?start="
        + str(starting_index)
        + "&end="
        + str(ending_index),
        jobs_manager,
    )

    if result is None:
        logger.error(
            "ERROR on request with starting index "
            + str(starting_index)
            + " and ending index "
            + str(ending_index)
        )
        time.sleep(600)
        result = make_https_request(
            logger,
            url
            + "/ct/v1/get-entries?start="
            + str(starting_index)
            + "&end="
            + str(ending_index),
            jobs_manager,
        )

    if result is None:
        jobs_manager.record_job_error()
        exit(1)

    return json.loads(result)


def fetch_starting_index(ct_collection, source):
    """
    Try to determine if we have the IDs from previous searches of this log.
    This will allow us to limit log searches to only those records that
    have been added since the last matched certificate ID.
    """
    result = (
        ct_collection.find(
            {source + "_id": {"$exists": True}}, {source + "_id": 1, "_id": 0}
        )
        .sort([(source + "_id", -1)])
        .limit(1)
    )

    if result.count() == 0:
        return 0

    for entry in result:
        return entry[source + "_id"]


def read_leaf_header(leaf):
    """
    Parse the leaf header
    """
    header = {}
    header["version"] = int(leaf[0])
    header["merkle_leaf_type"] = int(leaf[1])
    header["timestamp"] = int.from_bytes(leaf[2:10], "big")
    header["LogEntryType"] = int.from_bytes(leaf[10:12], "big")
    header["Entry"] = leaf[12:]
    return header


def get_cert_from_leaf(logger, leaf):
    """
    Extract the certificate from the certificate leaf
    """
    header = read_leaf_header(base64.b64decode(leaf))

    cert_type = header["LogEntryType"]
    if cert_type == 1:
        # Precertificates processed separately
        return None, cert_type

    cert_length = int.from_bytes(header["Entry"][0:3], "big")
    cert = header["Entry"][3:]

    if cert_length != len(cert) - 2:
        logger.warning("Error processing leaf: Length mismatch.")
        logger.warning(
            "CERT_LENGTH: " + str(cert_length) + " LENGTH: " + str(len(cert))
        )
        return None, cert_type

    return cert, cert_type


def get_cert_from_extra_data(extra_data):
    """
    The certificate is located in the extra data field for pre-certificates
    """
    data = base64.b64decode(extra_data)
    cert_length = int.from_bytes(data[0:3], "big")
    cert = data[3 : cert_length + 4]

    return cert


def check_org_relevancy(cert, ssl_orgs):
    """
    Check to see if the certificate is relevant to our organization.
    """
    if "subject_organization_name" in cert:
        for org in cert["subject_organization_name"]:
            if org in ssl_orgs:
                return True


def check_zone_relevancy(cert, zones):
    """
    Find the related zones within the certificate
    """
    cert_zones = []

    if "subject_common_names" in cert:
        for cn in cert["subject_common_names"]:
            for zone in zones:
                if cn == zone or cn.endswith("." + zone):
                    if zone not in cert_zones:
                        cert_zones.append(zone)

    if "subject_dns_names" in cert:
        for cn in cert["subject_dns_names"]:
            for zone in zones:
                if cn == zone or cn.endswith("." + zone):
                    if zone not in cert_zones:
                        cert_zones.append(zone)

    return cert_zones


def insert_certificate(cert, source, ct_collection, cert_zones):
    """
    Insert or update the record in the database as needed.
    """

    if (
        ct_collection.find({"fingerprint_sha256": cert["fingerprint_sha256"]}).count()
        == 0
    ):
        ct_collection.insert_one(cert)
    else:
        ct_collection.update_many(
            {"fingerprint_sha256": cert["fingerprint_sha256"]},
            {
                "$set": {
                    source + "_id": cert[source + "_id"],
                    "ct_log_type": cert["ct_log_type"],
                    "zones": cert_zones,
                    "marinus_updated": datetime.now(),
                },
                "$addToSet": {"sources": source},
            },
        )


def write_file(logger, cert, save_location, save_type, source):
    """
    Write the file to disk.
    """
    try:
        c_file = crypto.load_certificate(
            crypto.FILETYPE_PEM,
            "-----BEGIN CERTIFICATE-----\n"
            + cert["raw"]
            + "\n-----END CERTIFICATE-----",
        )
    except:
        # Once in awhile, it won't decode as PEM. Trying ASN1...
        try:
            c_file = crypto.load_certificate(
                crypto.FILETYPE_ASN1, base64.b64decode(cert["raw"])
            )
        except:
            logger.error(
                "ERROR: Couldn't write the file but it is saved in the DB. Skipping the write to disk operation."
            )
            return

    if save_type == "PEM":
        new_file = crypto.dump_certificate(crypto.FILETYPE_PEM, c_file)
        fh = open(
            save_location + "ct_" + source + "/" + str(cert[source + "_id"]) + ".pem",
            "wb",
        )
        fh.write(new_file)
        fh.close()
    else:
        new_file = crypto.dump_certificate(crypto.FILETYPE_ASN1, c_file)
        fh = open(
            save_location + "ct_" + source + "/" + str(cert[source + "_id"]) + ".der",
            "wb",
        )
        fh.write(new_file)
        fh.close()


def check_save_location(save_location, source):
    """
    Check to see if the directory exists.
    If the directory does not exist, it will automatically create it.
    """
    if not os.path.exists(save_location + "ct_" + source):
        os.makedirs(save_location + "ct_" + source)


def main():
    """
    Begin Main...
    """
    logger = LoggingUtil.create_log(__name__)

    now = datetime.now()
    print("Starting: " + str(now))
    logger.info("Starting...")

    # Make database connections
    mongo_connector = MongoConnector.MongoConnector()
    ct_collection = mongo_connector.get_certificate_transparency_connection()
    config_collection = mongo_connector.get_config_connection()
    x509parser = X509Parser.X509Parser()

    zones = ZoneManager.get_distinct_zones(mongo_connector)
    result = config_collection.find_one({}, {"SSL_Orgs": 1, "_id": 0})
    ssl_orgs = result["SSL_Orgs"]

    # Defaults
    save_location = "/mnt/workspace/"
    download_method = "dbAndSave"
    save_type = "PEM"

    parser = argparse.ArgumentParser(
        description="Download certificate information from the provide CT Log."
    )
    parser.add_argument(
        "--log_source",
        required=True,
        help="Indicates which log to query based on values in the x509Parser library",
    )
    parser.add_argument(
        "--include_precerts",
        action="store_true",
        help="Include pre-certificates which are not finalized",
    )
    parser.add_argument(
        "--download_methods",
        choices=["dbAndSave", "dbOnly"],
        default=download_method,
        help="Indicates whether to download the raw files or just save to the database",
    )
    parser.add_argument(
        "--starting_index",
        required=False,
        default=-1,
        type=int,
        help="Force the script to start at specific index within the log.",
    )
    parser.add_argument(
        "--cert_save_location",
        required=False,
        default=save_location,
        help="Indicates where to save the certificates on disk when choosing dbAndSave",
    )
    parser.add_argument(
        "--save_type",
        choices=["PEM", "ASN1"],
        default=save_type,
        help="Indicates which format to use for the data. The default is PEM",
    )
    args = parser.parse_args()

    source = args.log_source
    try:
        ct_log_map = x509parser.CT_LOG_MAP[source]
    except:
        logger.error("ERROR: UNKNOWN LOG SOURCE: " + source)
        exit(1)

    if args.cert_save_location:
        save_location = args.cert_save_location
        if not save_location.endswith("/"):
            save_location = save_location + "/"

    if args.download_methods:
        download_method = args.download_methods
        check_save_location(save_location, source)

    if args.save_type:
        save_type = args.save_type

    jobs_manager = JobsManager.JobsManager(mongo_connector, "ct_log-" + source)
    jobs_manager.record_job_start()

    if args.starting_index == -1:
        starting_index = fetch_starting_index(ct_collection, source)
    else:
        starting_index = args.starting_index
    logger.info("Starting Index: " + str(starting_index))

    sth_data = fetch_sth(logger, "https://" + ct_log_map["url"], jobs_manager)
    logger.info("Tree size: " + str(sth_data["tree_size"]))

    current_index = starting_index
    while current_index < sth_data["tree_size"]:
        ending_index = current_index + 256
        if ending_index > sth_data["tree_size"]:
            ending_index = sth_data["tree_size"]

        logger.debug(
            "Checking from index: "
            + str(current_index)
            + " to index "
            + str(ending_index)
        )
        certs = fetch_certificate_batch(
            logger,
            "https://" + ct_log_map["url"],
            current_index,
            ending_index,
            jobs_manager,
        )

        for entry in certs["entries"]:
            der_cert, cert_type = get_cert_from_leaf(logger, entry["leaf_input"])
            if der_cert is None and cert_type == 1 and not args.include_precerts:
                current_index = current_index + 1
                continue
            elif der_cert is None and cert_type == 0:
                current_index = current_index + 1
                continue
            elif der_cert is None and cert_type == 1:
                der_cert = get_cert_from_extra_data(entry["extra_data"])

            cert = x509parser.parse_data(der_cert, source)
            if cert is None:
                logger.warning("Skipping certificate index: " + str(current_index))
                current_index = current_index + 1
                continue

            if cert_type == 1:
                cert["ct_log_type"] = "PRE-CERTIFICATE"
            else:
                cert["ct_log_type"] = "CERTIFICATE"

            cert_zones = check_zone_relevancy(cert, zones)

            if check_org_relevancy(cert, ssl_orgs) or cert_zones != []:
                cert[source + "_id"] = current_index
                cert["zones"] = cert_zones
                logger.info(
                    "Adding "
                    + source
                    + " id: "
                    + str(current_index)
                    + " SHA256: "
                    + cert["fingerprint_sha256"]
                )
                insert_certificate(cert, source, ct_collection, cert_zones)

                if download_method == "dbAndSave":
                    write_file(logger, cert, save_location, save_type, source)

            current_index = current_index + 1

    # Set isExpired for any entries that have recently expired.
    ct_collection.update_many(
        {"not_after": {"$lt": datetime.utcnow()}, "isExpired": False},
        {"$set": {"isExpired": True}},
    )

    jobs_manager.record_job_complete()

    now = datetime.now()
    print("Ending: " + str(now))
    logger.info("Complete.")


if __name__ == "__main__":
    main()
