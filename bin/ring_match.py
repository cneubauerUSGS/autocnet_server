import argparse
import json
import os
import pickle
import sys
import time

import numpy as np
import ogr
import h5py
import pyproj
from geoalchemy2.shape import to_shape
from redis import StrictRedis

import yaml

#Load the config file
with open(os.environ['autocnet_config'], 'r') as f:
    config = yaml.load(f)

# Patch in dev. versions if requested.
acp = config.get('developer', {}).get('autocnet_path', None)
if acp:
    sys.path.insert(0, acp)

asp = config.get('developer', {}).get('autocnet_server_path', None)
if asp:
    sys.path.insert(0, asp)

from autocnet_server.camera.csm_camera import ecef_to_latlon
from autocnet_server.db import connection
from autocnet_server.db.model import Keypoints, Matches, Cameras, Edges
from autocnet_server.utils.utils import slurm_walltime_to_seconds
from autocnet.matcher.cpu_ring_matcher import ring_match, add_correspondences
from autocnet.io.keypoints import from_hdf


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--ringradius', default=100)
    parser.add_argument('-m', '--maxradius', default=1200)
    return parser.parse_args()

def match(msg, args):

    # Get the s/d ids from the message queue and then grab the paths from the
    # DB
    source_id = msg['sidx']
    destin_id = msg['didx']
    target_points = msg['target_points']
    tolerance = msg['tolerance']

    print('Processing Edge: ({},{})'.format(source_id, destin_id))
    session, _ = connection.new_connection()
    sfile = session.query(Keypoints).filter(Keypoints.image_id == source_id).first().path
    dfile = session.query(Keypoints).filter(Keypoints.image_id == destin_id).first().path
    camera = pickle.loads(session.query(Cameras).filter(Cameras.image_id == source_id).first().camera)
    session.close()

    # Grab the reference and target keypoints
    ref_kps, ref_desc = from_hdf(sfile)
    tar_kps, tar_desc = from_hdf(dfile)

    # Default message
    data = {'success':False,
            'sidx': msg['sidx'], 'didx': msg['didx'],
            'callback':'ring_matcher_callback'}

    ref_feats = ref_kps[['x', 'y', 'xm', 'ym', 'zm']].values
    tar_feats = tar_kps[['x', 'y', 'xm', 'ym', 'zm']].values

    # Ring Match
    _, _, pidx, ring = ring_match(ref_feats, tar_feats,
                                  ref_desc, tar_desc,
                                  ring_radius=args.ringradius,
                                  max_radius=args.maxradius,
                                  target_points=target_points,
                                  tolerance_val=tolerance)


    if pidx is None:
        print('Unable to find a solution.')
        return data

    # Now densify the matches if a ring has been found
    print('Initial Pass Resulted in {} matches'.format(len(pidx)))
    print('Distance ring: {}'.format(ring))

    in_feats = ref_feats[pidx[:,0]][:,:2]  # all reference points[those selected by ring matcher][x,y coords]
    xextent = (np.min(in_feats[:,1]), np.max(in_feats[:,1]))
    yextent = (np.min(in_feats[:,0]), np.max(in_feats[:,0]))
    refs_to_add = add_correspondences(in_feats,
                                      ref_feats, tar_feats,
                                      ref_desc, tar_desc,
                                      xextent, yextent, ring,
                                      8, 8, target_points=15,
                                      search_radius=int(args.ringradius / 3),
                                      max_search_radius=args.ringradius)
    refs_to_add = [i for i in refs_to_add if len(i)]
    if refs_to_add:
        print('Adding {} correspondences'.format(len(refs_to_add)))
        stacked_refs_to_add = np.vstack(refs_to_add)
        pidx = np.vstack((pidx, stacked_refs_to_add))
        # Get the unique rows: https://stackoverflow.com/questions/31097247/remove-duplicate-rows-of-a-numpy-array
        # Perform lex sort and get sorted data
        sorted_idx = np.lexsort(pidx.T)
        sorted_data =  pidx[sorted_idx,:]
        # Get unique row mask
        row_mask = np.append([True],np.any(np.diff(sorted_data,axis=0),1))
        # Get unique rows
        pidx = sorted_data[row_mask]
    else:
        print('no additional references to add')
    
    # Check for duplicates
    l = pidx[:,1].tolist()
    clean = [i for i, x in enumerate(l) if l.count(x) == 1]
    if len(clean) < len(pidx):
        print('Col1: ', pidx)
    pidx = pidx[clean, :]

    l = pidx[:,0].tolist()
    clean = [i for i, x in enumerate(l) if l.count(x) == 1]
    if len(clean) < len(pidx):
        print('Col0 ', pidx)
    pidx = pidx[clean, :]

    # Package the data to round trip to the server
    data['success'] = True
    return data, (pidx, ref_feats[pidx[:,0]], tar_feats[pidx[:,1]], ring, camera)

def finalize(data, queue, msg):

    for k, v in data.items():
        if isinstance(v, np.ndarray):
            data[k] = v.tolist()

    queue.rpush(config['redis']['completed_queue'], json.dumps(data))

    # Now that work is done, clean out the 'working queue'
    queue.lrem(config['redis']['working_queue'], 0, json.dumps(msg))

def write_to_db(pidx, refkps, tarkps, ring, camera, msg):
    
    to_add = []
    for i, row in enumerate(pidx):
        sidx = int(row[0])
        didx = int(row[1])
        sx = float(refkps[i][0])
        sy = float(refkps[i][1])
        dx = float(tarkps[i][0])
        dy = float(tarkps[i][1])
        # Use the source camera to project the sx, sy to ground
        gnd = camera.imageToGround(sy, sx, 0)
        lon, lat, alt = ecef_to_latlon(gnd)
        # TODO: Hard coded srid needs to be set at the project level
        geom = 'SRID=949900;POINTZ({} {} {})'.format(lon, lat, alt)
        m = Matches(source=msg['sidx'], source_idx=sidx,
                    destination=msg['didx'], destination_idx=didx,
                    lat=float(lat), lon=float(lon), geom=geom,
                    source_x=sx, source_y=sy,
                    destination_x=dx, destination_y=dy)
        to_add.append(m) 
    
    e = Edges(source=msg['sidx'], destination=msg['didx'], ring=ring)

    session, _ = connection.new_connection()
    session.begin()
    session.bulk_save_objects(to_add)
    session.add(e)
    session.commit()
    session.close()

if __name__ == '__main__':
    args = parse_args()
    queue = StrictRedis( host="smalls", port=8000, db=0)
    # Load the message out of the processing queue and add a max processing time key
    msg = json.loads(queue.rpop(config['redis']['processing_queue']))
    msg['max_time'] = time.time() + slurm_walltime_to_seconds(msg['walltime'])
    
    # Push the message to the processing queue with the updated max_time
    queue.lpush(config['redis']['working_queue'], json.dumps(msg))

    # Apply the matcher
    data, to_db = match(msg, args)
    
    # Write to the database if successful
    if data['success']:
        write_to_db(*to_db, msg)
    
    # Alert the caller on failure to relaunch with next parameter set
    finalize(data, queue, msg)
