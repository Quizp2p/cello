import datetime
import logging
import os
import random
import sys
import time

from threading import Thread
from pymongo.collection import ReturnDocument

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from common import db, cleanup_container_host, LOG_LEVEL, LOG_TYPES, \
    CLUSTER_SIZES, CLUSTER_API_PORT_START, CONSENSUS_TYPES, log_handler, \
    LOGGING_LEVEL_CLUSTERS, check_daemon, detect_daemon_type, \
    reset_container_host, setup_container_host

from modules import cluster

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)
logger.addHandler(log_handler)


def check_status(func):
    def wrapper(self, *arg):
        if not self.is_active(*arg):
            logger.warn("Host inactive")
            return False
        else:
            return func(self, *arg)
    return wrapper


class HostHandler(object):
    """ Main handler to operate the Docker hosts
    """
    def __init__(self):
        self.col = db["host"]

    def create(self, name, daemon_url, capacity=1,
               log_level=LOGGING_LEVEL_CLUSTERS[0],
               log_type=LOG_TYPES[0], log_server="", fillup=False,
               schedulable="false", serialization=True):
        """ Create a new docker host node

        A docker host is potentially a single node or a swarm.
        Will full fill with clusters of given capacity.

        :param name: name of the node
        :param daemon_url: daemon_url of the host
        :param capacity: The number of clusters to hold
        :param log_type: type of the log
        :param log_level: level of the log
        :param log_server: server addr of the syslog
        :param fillup: Whether fillup after creation
        :param schedulable: Whether can schedule cluster request to it
        :param serialization: whether to get serialized result or object
        :return: True or False
        """
        logger.debug("Create host: name={}, daemon_url={}, capacity={}, "
                     "log={}/{}, fillup={}, schedulable={}"
                     .format(name, daemon_url, capacity, log_type,
                             log_server, fillup, schedulable))
        if not daemon_url.startswith("tcp://"):
            daemon_url = "tcp://" + daemon_url

        if self.col.find_one({"daemon_url": daemon_url}):
            logger.warn("{} already existed in db".format(daemon_url))
            return {}

        if "://" not in log_server:
            log_server = "udp://" + log_server
        if log_type == LOG_TYPES[0]:
            log_server = ""
        if check_daemon(daemon_url):
            logger.warn("The daemon_url is active:" + daemon_url)
            status = "active"
        else:
            logger.warn("The daemon_url is inactive:" + daemon_url)
            status = "inactive"

        detected_type = detect_daemon_type(daemon_url)

        if not setup_container_host(detected_type, daemon_url):
            logger.warn("{} cannot be setup".format(name))
            return {}

        h = {
            'name': name,
            'daemon_url': daemon_url,
            'create_ts': datetime.datetime.now(),
            'capacity': capacity,
            'status': status,
            'clusters': [],
            'type': detected_type,
            'log_level': log_level,
            'log_type': log_type,
            'log_server': log_server,
            'schedulable': schedulable
        }
        hid = self.col.insert_one(h).inserted_id  # object type
        host = self.db_update_one(
            {"_id": hid},
            {"$set": {"id": str(hid)}})

        if capacity > 0 and fillup:  # should fillup it
            self.fillup(str(hid))

        if serialization:
            return self._serialize(host)
        else:
            return host

    def get_by_id(self, id):
        """ Get a host

        :param id: id of the doc
        :return: serialized result or obj
        """
        logger.debug("Get a host with id=" + id)
        ins = self.col.find_one({"id": id})
        if not ins:
            logger.warn("No cluster found with id=" + id)
            return {}
        return self._serialize(ins)

    def update(self, id, d):
        """ Update a host

        TODO: may check when changing host type

        :param id: id of the host
        :param d: dict to use as updated values
        :return: serialized result or obj
        """
        logger.debug("Get a host with id=" + id)
        h_old = self.get_by_id(id)
        if not h_old:
            logger.warn("No host found with id=" + id)
            return {}

        if "daemon_url" in d and not d["daemon_url"].startswith("tcp://"):
            d["daemon_url"] = "tcp://" + d["daemon_url"]

        if "capacity" in d:
            d["capacity"] = int(d["capacity"])
        if d["capacity"] < len(h_old.get("clusters")):
            logger.warn("Cannot set cap smaller than running clusters")
            return {}
        if "log_server" in d and "://" not in d["log_server"]:
            d["log_server"] = "udp://" + d["log_server"]
        if "log_type" in d and d["log_type"] == LOG_TYPES[0]:
            d["log_server"] = ""
        h_new = self.db_set_by_id(id, d)
        return self._serialize(h_new)

    def list(self, filter_data={}):
        """ List hosts with given criteria

        :param filter_data: Image with the filter properties
        :return: iteration of serialized doc
        """
        hosts = self.col.find(filter_data)
        return list(map(self._serialize, hosts))

    def delete(self, id):
        """ Delete a host instance

        :param id: id of the host to delete
        :return:
        """
        logger.debug("Delete a host with id={0}".format(id))

        h = self.get_by_id(id)
        if not h:
            logger.warn("Cannot delete non-existed host")
            return False
        if h.get("clusters", ""):
            logger.warn("There are clusters on that host, cannot delete.")
            return False
        cleanup_container_host(h.get("daemon_url"))
        self.col.delete_one({"id": id})
        return True

    @check_status
    def fillup(self, id):
        """
        Fullfil a host with clusters to its capacity limit

        :param id: host id
        :return: True or False
        """
        logger.debug("fillup host with id = {}".format(id))
        host = self.get_by_id(id)
        if not host:
            return False
        num_new = host.get("capacity") - len(host.get("clusters"))
        if num_new <= 0:
            logger.warn("host already full")
            return True

        free_ports = cluster.cluster_handler.find_free_api_ports(id, num_new)
        logger.debug("Free_ports = {}".format(free_ports))

        def create_cluster_work(port):
            cluster_name = "{}_{}".format(host.get("name"),
                                          (port - CLUSTER_API_PORT_START))
            consensus_plugin, consensus_mode = random.choice(CONSENSUS_TYPES)
            cluster_size = random.choice(CLUSTER_SIZES)
            cid = cluster.cluster_handler.create(name=cluster_name, host_id=id,
                                         api_port=port,
                                         consensus_plugin=consensus_plugin,
                                         consensus_mode=consensus_mode,
                                         size=cluster_size)
            if cid:
                logger.debug("Create cluster %s with id={}".format(
                    cluster_name, cid))
            else:
                logger.warn("Create cluster failed")
        for p in free_ports:
            t = Thread(target=create_cluster_work, args=(p,))
            t.start()
            time.sleep(1.0)

        return True

    @check_status
    def clean(self, id):
        """
        Clean a host's free clusters.

        :param id: host id
        :return: True or False
        """
        logger.debug("clean host with id = {}".format(id))
        host = self.get_by_id(id)
        if not host:
            return False
        if len(host.get("clusters")) <= 0:
            return True

        host = self.db_set_by_id(id, schedulable=False)

        for cid in host.get("clusters"):
            t = Thread(target=cluster.cluster_handler.delete, args=(cid,))
            t.start()
            time.sleep(0.2)
        self.db_set_by_id(id, schedulable=True)

        return True

    @check_status
    def reset(self, id):
        """
        Clean a host's free clusters.

        :param id: host id
        :return: True or False
        """
        logger.debug("clean host with id = {}".format(id))
        host = self.get_by_id(id)
        if not host or len(host.get("clusters")) > 0:
            logger.warn("no resettable host is found with id ={}".format(id))
            return False
        return reset_container_host(host_type=host.get("type"),
                                    daemon_url=host.get("daemon_url"))

    def refresh_status(self, id):
        """
        Refresh the status of the host by detection

        :param host: the host to update status
        :return: Updated host
        """
        host = self.get_by_id(id)
        if not host:
            logger.warn("No host found with id=" + id)
            return {}
        if not check_daemon(host.get("daemon_url")):
            logger.warn("Host {} is inactive".format(id))
            return self.db_set_by_id(id, status="inactive")
        else:
            return self.db_set_by_id(id, status="active")

    def is_active(self, host_id):
        """
        Update status of the host

        :param host_id: the id of the host to update status
        :return: Updated host
        """
        host = self.get_by_id(host_id)
        if not host:
            logger.warn("invalid host is given")
            return False
        return host.get("status") == "active"

    def get_active_host_by_id(self, id):
        """
        Check if id exists, and status is active. Otherwise update to inactive.

        :param id: host id
        :return: host or None
        """
        logger.debug("check host with id = {}".format(id))
        host = self.col.find_one({"id": id, "status": "active"})
        if not host:
            logger.warn("No active host found with id=" + id)
            return {}
        return self._serialize(host)

    def _serialize(self, doc, keys=['id', 'name', 'daemon_url', 'capacity',
                                    'type', 'create_ts', 'status',
                                    'schedulable', 'clusters', 'log_level',
                                    'log_type', 'log_server']):
        """ Serialize an obj

        :param doc: doc to serialize
        :param keys: filter which key in the results
        :return: serialized obj
        """
        result = {}
        if doc:
            for k in keys:
                result[k] = doc.get(k, '')
        return result

    def db_set_by_id(self, id, **kwargs):
        """
        Set the key:value pairs to the data
        :param id: Which host to update
        :param kwargs: kv pairs
        :return: The updated host json dict
        """
        return self.db_update_one({"id": id}, {"$set": kwargs})

    def db_update_one(self, filter, operations, after=True):
        """
        Update the data into the active db

        :param filter: Which instance to update, e.g., {"id": "xxx"}
        :param operations: data to update to db, e.g., {"$set": {}}
        :param after: return AFTER or BEFORE
        :return: The updated host json dict
        """
        if after:
            return_type = ReturnDocument.AFTER
        else:
            return_type = ReturnDocument.BEFORE
        doc = self.col.find_one_and_update(
            filter, operations, return_document=return_type)
        return self._serialize(doc)


host_handler = HostHandler()