# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Validate a trained YOLOv5 detection model on a detection dataset

Usage:
    $ python val.py weights/best.pt --data data/pano.yaml --skip_evaluation --save_blurred_image

Usage - formats:
    $ python val.py --weights yolov5s.pt                 # PyTorch
                              yolov5s.torchscript        # TorchScript
                              yolov5s.onnx               # ONNX Runtime or OpenCV DNN with --dnn
                              yolov5s_openvino_model     # OpenVINO
                              yolov5s.engine             # TensorRT
                              yolov5s.mlmodel            # CoreML (macOS-only)
                              yolov5s_saved_model        # TensorFlow SavedModel
                              yolov5s.pb                 # TensorFlow GraphDef
                              yolov5s.tflite             # TensorFlow Lite
                              yolov5s_edgetpu.tflite     # TensorFlow Edge TPU
                              yolov5s_paddle_model       # PaddlePaddle
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from tqdm import tqdm
from datetime import datetime

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from yolov5.baas_utils.database_handler import DBConfigSQLAlchemy
from yolov5.baas_utils.database_tables import DetectionInformation, ImageProcessingStatus, BatchRunInformation
from yolov5.baas_utils.date_utils import extract_upload_date, get_current_time
from yolov5.baas_utils.error_handling import exception_handler

from yolov5.models.common import DetectMultiBackend
from yolov5.utils.callbacks import Callbacks
from yolov5.utils.dataloaders import create_dataloader
from yolov5.utils.general import (
    LOGGER,
    TQDM_BAR_FORMAT,
    Profile,
    check_dataset,
    check_img_size,
    check_requirements,
    check_yaml,
    coco80_to_coco91_class,
    colorstr,
    cv2,
    increment_path,
    non_max_suppression,
    print_args,
    scale_boxes,
    xywh2xyxy,
    xyxy2xywh,
)
from yolov5.utils.metrics import ConfusionMatrix, TaggedConfusionMatrix, ap_per_class, box_iou
from yolov5.utils.plots import output_to_target, plot_images, plot_val_study
from yolov5.utils.torch_utils import select_device, smart_inference_mode
from torchvision.utils import save_image

# Use the following repo for local run https://github.com/Computer-Vision-Team-Amsterdam/yolov5-local-docker
LOCAL_RUN = False


def is_area_positive(x1, y1, x2, y2):
    if x1 == x2 or y1 == y2:
        return False
    return True


def save_one_txt_and_one_json(predn, save_conf, shape, file, json_file, confusion_matrix):
    """
    Save format for running tagged validation

    Args:
        confusion_matrix: tagged confusion matrix instance
        json_file: path to save json dict
        predn: predictions tensor
        save_conf: whether to store the confidence of the predictions
        shape: image shape
        file: path of txt file

    Returns:

    """
    pred_boxes = []
    pred_classes = []
    gn = torch.tensor(shape)[[1, 0, 1, 0]]  # normalization gain whwh
    for *xyxy, conf, cls in predn.tolist():
        xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
        line = (cls, *xywh, conf) if save_conf else (cls, *xywh)  # label format
        pred_boxes.append(xywh)
        pred_classes.append(int(cls))
        with open(file, 'a') as f:
            f.write(('%g ' * len(line)).rstrip() % line + '\n')

    confusion_matrix.pred_boxes = pred_boxes
    confusion_matrix.pred_classes = pred_classes

    with open(json_file, 'w') as fp:
        json.dump(confusion_matrix.get_tagged_dict(), fp)
    LOGGER.info(f'saved json at {json_file}')


def save_one_txt(predn, save_conf, shape, file):
    # Save one txt result
    gn = torch.tensor(shape)[[1, 0, 1, 0]]  # normalization gain whwh
    for *xyxy, conf, cls in predn.tolist():
        xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
        line = (cls, *xywh, conf) if save_conf else (cls, *xywh)  # label format
        with open(file, 'a') as f:
            f.write(('%g ' * len(line)).rstrip() % line + '\n')


def save_one_json(predn, jdict, path, class_map):
    # Save one JSON result {"image_id": 42, "category_id": 18, "bbox": [258.15, 41.29, 348.26, 243.78], "score": 0.236}
    image_id = path.stem
    box = xyxy2xywh(predn[:, :4])  # xywh
    box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
    for p, b in zip(predn.tolist(), box.tolist()):
        jdict.append({
            'image_id': image_id,
            'category_id': class_map[int(p[5])],
            'bbox': [round(x, 3) for x in b],
            'score': round(p[4], 5)})


def process_batch(detections, labels, iouv):
    """
    Return correct prediction matrix
    Arguments:
        detections (array[N, 6]), x1, y1, x2, y2, conf, class
        labels (array[M, 5]), class, x1, y1, x2, y2
    Returns:
        correct (array[N, 10]), for 10 IoU levels
    """
    correct = np.zeros((detections.shape[0], iouv.shape[0])).astype(bool)
    iou = box_iou(labels[:, 1:], detections[:, :4])
    correct_class = labels[:, 0:1] == detections[:, 5]
    for i in range(len(iouv)):
        x = torch.where((iou >= iouv[i]) & correct_class)  # IoU > threshold and classes match
        if x[0].shape[0]:
            matches = torch.cat((torch.stack(x, 1), iou[x[0], x[1]][:, None]), 1).cpu().numpy()  # [label, detect, iou]
            if x[0].shape[0] > 1:
                matches = matches[matches[:, 2].argsort()[::-1]]
                matches = matches[np.unique(matches[:, 1], return_index=True)[1]]
                # matches = matches[matches[:, 2].argsort()[::-1]]
                matches = matches[np.unique(matches[:, 0], return_index=True)[1]]
            correct[matches[:, 1].astype(int), i] = True
    return torch.tensor(correct, dtype=torch.bool, device=iouv.device)


@exception_handler
@smart_inference_mode()
def run(
        data,
        weights=None,  # model.pt path(s)
        batch_size=32,  # batch size
        imgsz=640,  # inference size (pixels)
        conf_thres=0.001,  # confidence threshold
        iou_thres=0.6,  # NMS IoU threshold
        max_det=300,  # maximum detections per image
        task='val',  # train, val, test, speed or study
        device='',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
        workers=8,  # max dataloader workers (per RANK in DDP mode)
        single_cls=False,  # treat as single-class dataset
        augment=False,  # augmented inference
        verbose=False,  # verbose output
        save_txt=False,  # save results to *.txt
        save_hybrid=False,  # save label+prediction hybrid results to *.txt
        save_conf=False,  # save confidences in --save-txt labels
        save_json=False,  # save a COCO-JSON results file
        project=ROOT / 'runs/val',  # save to project/name
        name='exp',  # save to project/name
        exist_ok=False,  # existing project/name ok, do not increment
        half=True,  # use FP16 half-precision inference
        dnn=False,  # use OpenCV DNN for ONNX inference
        model=None,
        dataloader=None,
        save_dir=Path(''),
        plots=True,
        callbacks=Callbacks(),
        compute_loss=None,
        tagged_data=False,
        skip_evaluation=False,
        save_blurred_image=False,
        customer_name='',
        run_id='default_run_id',
        db_username='',
        db_hostname='',
        db_name='',
        start_time='',
        no_inverted_colors=False):
    # Initialize/load model and set device
    training = model is not None
    if training:  # called by train.py
        device, pt, jit, engine = next(model.parameters()).device, True, False, False  # get model device, PyTorch model
        half &= device.type != 'cpu'  # half precision only supported on CUDA
        model.half() if half else model.float()
    else:  # called directly
        device = select_device(device, batch_size=batch_size)

        # Directories
        if LOCAL_RUN:
            save_dir = Path('/container/landing_zone/output')
            input_dir = Path('/container/landing_zone/input_structured/')
        else:
            save_dir = increment_path(Path(project) / name, exist_ok=exist_ok)  # increment run
            input_dir = ''

        (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
        if tagged_data:
            (save_dir / 'labels_tagged' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)
            # Load model
        model = DetectMultiBackend(weights, device=device, dnn=dnn, data=data, fp16=half)
        stride, pt, jit, engine = model.stride, model.pt, model.jit, model.engine
        imgsz = check_img_size(imgsz, s=stride)  # check image size
        half = model.fp16  # FP16 supported on limited backends with CUDA
        if engine:
            batch_size = model.batch_size
        else:
            device = model.device
            if not (pt or jit):
                batch_size = 1  # export.py models default to batch-size 1
                LOGGER.info(f'Forcing --batch-size 1 square inference (1,3,{imgsz},{imgsz}) for non-PyTorch models')

        # Data
        data = check_dataset(data)  # check

    # Configure
    model.eval()
    cuda = device.type != 'cpu'
    is_coco = isinstance(data.get('val'), str) and data['val'].endswith(f'coco{os.sep}val2017.txt')  # COCO dataset
    nc = 1 if single_cls else int(data['nc'])  # number of classes
    iouv = torch.linspace(0.5, 0.95, 10, device=device)  # iou vector for mAP@0.5:0.95
    niou = iouv.numel()

    if skip_evaluation:
        # Validate if database credentials are provided
        if not db_username or not db_name or not db_hostname:
            raise ValueError('Please provide database credentials.')

        # Create a DBConfigSQLAlchemy object
        db_config = DBConfigSQLAlchemy(db_username, db_hostname, db_name)
        # Create the database connection
        db_config.create_connection()

    # Dataloader
    if not training:
        if pt and not single_cls:  # check --weights are trained on --data
            ncm = model.model.nc
            assert ncm == nc, f'{weights} ({ncm} classes) trained on different --data than what you passed ({nc} ' \
                              f'classes). Pass correct combination of --weights and --data that are trained together.'
        model.warmup(imgsz=(1 if pt else batch_size, 3, imgsz, imgsz))  # warmup
        pad, rect = (0.0, False) if task == 'speed' else (0.5, pt)  # square inference for benchmarks
        task = task if task in ('train', 'val', 'test') else 'val'  # path to train/val/test images

        if skip_evaluation:
            # Define the processing statuses
            processing_statuses = ['processed']

            # Perform database operations using the 'session'
            # The session will be automatically closed at the end of this block
            with db_config.managed_session() as session:
                try:
                    # Construct the query to get all rows with a certain processing status
                    query = session.query(
                        func.date(ImageProcessingStatus.image_upload_date).label('upload_date'),
                        ImageProcessingStatus.image_filename
                    ) \
                        .filter(
                        ImageProcessingStatus.image_customer_name == customer_name,
                        ImageProcessingStatus.processing_status.in_(processing_statuses)
                    )

                    # Execute the query and fetch the results
                    result = query.all()
                except SQLAlchemyError as e:
                    # Handle the exception
                    db_config.close_connection()
                    raise e

            # Extract the processed images from the result
            processed_images = [
                f'{input_dir / row.upload_date / row.image_filename}'
                if input_dir else f'{row.upload_date}/{row.image_filename}' for row in result]
        else:
            processed_images = []

        image_files, dataloader, _ = create_dataloader(data[task],
                                                       processed_images,
                                                       input_dir,
                                                       imgsz,
                                                       batch_size,
                                                       stride,
                                                       single_cls,
                                                       pad=pad,
                                                       rect=rect,
                                                       workers=workers,
                                                       prefix=colorstr(f'{task}: '))

        if skip_evaluation:
            # Perform database operations using the 'session'
            # The session will be automatically closed at the end of this block
            with db_config.managed_session() as session:
                # Lock the images that we are processing in this run with the state "inprogress"
                for image_path in image_files:
                    # Get variables to later insert into the database
                    image_filename, image_upload_date = extract_upload_date(image_path)

                    # Create a new instance of the ImageProcessingStatus model
                    image_processing_status = ImageProcessingStatus(image_filename=image_filename,
                                                                    image_upload_date=image_upload_date,
                                                                    image_customer_name=customer_name,
                                                                    processing_status='inprogress')

                    # Add the instance to the session
                    session.add(image_processing_status)

    seen = 0
    if not tagged_data:
        confusion_matrix = ConfusionMatrix(nc=nc)
    names = model.names if hasattr(model, 'names') else model.module.names  # get class names
    if isinstance(names, (list, tuple)):  # old format
        names = dict(enumerate(names))
    class_map = coco80_to_coco91_class() if is_coco else list(range(1000))
    s = ('%22s' + '%11s' * 6) % ('Class', 'Images', 'Instances', 'P', 'R', 'mAP50', 'mAP50-95')
    tp, fp, p, r, f1, mp, mr, map50, ap50, map = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    dt = Profile(), Profile(), Profile()  # profiling times
    loss = torch.zeros(3, device=device)
    jdict, stats, ap, ap_class = [], [], [], []
    callbacks.run('on_val_start')
    pbar = tqdm(dataloader, desc=s, bar_format=TQDM_BAR_FORMAT)  # progress bar
    for batch_i, (im, targets, paths, shapes, im_orig) in enumerate(pbar):
        callbacks.run('on_val_batch_start')
        if tagged_data:
            confusion_matrix = TaggedConfusionMatrix(nc=nc)
        with dt[0]:
            if cuda:
                im = im.to(device, non_blocking=True)
                targets = targets.to(device)
            im = im.half() if half else im.float()  # uint8 to fp16/32
            im /= 255  # 0 - 255 to 0.0 - 1.0
            nb, _, height, width = im.shape  # batch size, channels, height, width

        # Inference
        with dt[1]:
            preds, train_out = model(im) if compute_loss else (model(im, augment=augment), None)

        # Loss
        if compute_loss:
            loss += compute_loss(train_out, targets)[1]  # box, obj, cls

        # NMS
        if tagged_data:
            targets[:, 2:-1] *= torch.tensor((width, height, width, height), device=device)  # to pixels
        else:
            targets[:, 2:] *= torch.tensor((width, height, width, height), device=device)  # to pixels
        lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
        with dt[2]:
            preds = non_max_suppression(preds,
                                        conf_thres,
                                        iou_thres,
                                        labels=lb,
                                        multi_label=True,
                                        agnostic=single_cls,
                                        max_det=max_det)

        # Dictionary to store detection flags for each image
        image_detections = {path: False for path in paths}  # Initialize all paths with False

        # Metrics
        for si, pred in enumerate(preds):
            # Update image detection flag
            image_detections[paths[si]] = len(pred) > 0

            labels = targets[targets[:, 0] == si, 1:]
            if tagged_data:
                tagged_labels = targets[:, -1]
                gt_boxes = targets[:, 2:-1] / torch.tensor((width, height, width, height), device=device)
            nl, npr = labels.shape[0], pred.shape[0]  # number of labels, predictions
            path, shape = Path(paths[si]), shapes[si][0]
            image_height, image_width = shape

            correct = torch.zeros(npr, niou, dtype=torch.bool, device=device)  # init
            seen += 1

            p = Path(path)  # to Path # TODO it is already Path
            is_wd_path = 'wd' in p.parts
            relative_path_in_azure_mounted_folder = Path('/'.join(p.parts[p.parts.index('wd') +
                                                                          2:])) if is_wd_path else None
            save_path = str(save_dir / (relative_path_in_azure_mounted_folder if is_wd_path else p.name))

            if npr == 0:
                if nl and not skip_evaluation:
                    stats.append((correct, *torch.zeros((2, 0), device=device), labels[:, 0]))
                    if plots:
                        confusion_matrix.process_batch(detections=None, labels=labels[:, 0])
                continue

            # Predictions
            if single_cls:
                pred[:, 5] = 0
            predn = pred.clone()
            pred_clone = pred.clone()
            scale_boxes(im[si].shape[1:], predn[:, :4], shape, shapes[si][1])  # native-space pred (this changes predn)

            # Evaluate
            if not skip_evaluation:
                if nl:
                    tbox = xywh2xyxy(labels[:, 1:5])  # target boxes
                    scale_boxes(im[si].shape[1:], tbox, shape, shapes[si][1])  # native-space labels
                    labelsn = torch.cat((labels[:, 0:1], tbox), 1)  # native-space labels
                    correct = process_batch(predn, labelsn, iouv)
                    if plots:
                        if tagged_data:
                            confusion_matrix.process_batch(predn, labelsn, gt_boxes, tagged_labels)
                        else:
                            confusion_matrix.process_batch(predn, labelsn)
                stats.append((correct, pred[:, 4], pred[:, 5], labels[:, 0]))  # (correct, conf, pcls, tcls)

            # Save/log
            if save_txt:
                if tagged_data:
                    save_one_txt_and_one_json(predn,
                                              save_conf,
                                              shape,
                                              file=save_dir / 'labels' / f'{path.stem}.txt',
                                              json_file=save_dir / 'labels_tagged' / f'{path.stem}.json',
                                              confusion_matrix=confusion_matrix)
                else:
                    save_one_txt(predn, save_conf, shape, file=save_dir / 'labels' / f'{path.stem}.txt')
                    # Print results
                    text = ''
                    for c in predn[:, 5].unique():
                        n = (predn[:, 5] == c).sum()  # detections per class
                        text += f"{n} {names[int(c)]}{'s' * (n > 1)}, "  # add to string
                    LOGGER.info(f'{path.stem}: {text}')
            if save_json:
                save_one_json(predn, jdict, path, class_map)  # append to COCO-JSON dictionary
            callbacks.run('on_val_image_end', pred, predn, path, names, im[si])

            if save_blurred_image:
                pred_clone[:, :4] = scale_boxes(im[si].shape[1:], pred_clone[:, :4], shape, shapes[si][1])
                for *xyxy, conf, cls in pred_clone.tolist():
                    x1, y1 = int(xyxy[0]), int(xyxy[1])
                    x2, y2 = int(xyxy[2]), int(xyxy[3])

                    if is_area_positive(x1, y1, x2, y2):
                        area_to_blur = im_orig[si][y1:y2, x1:x2]
                        blurred = cv2.GaussianBlur(area_to_blur, (135, 135), 0)
                        im_orig[si][y1:y2, x1:x2] = blurred

                        if skip_evaluation:
                            # Get variables to later insert into the database
                            image_filename, image_upload_date = extract_upload_date(paths[si])

                            # The session will be automatically closed at the end of this block
                            with db_config.managed_session() as session:
                                # Create an instance of DetectionInformation
                                detection_info = DetectionInformation(image_customer_name=customer_name,
                                                                      image_upload_date=image_upload_date,
                                                                      image_filename=image_filename,
                                                                      has_detection=True,
                                                                      class_id=int(cls),
                                                                      x_norm=x1,
                                                                      y_norm=y1,
                                                                      w_norm=x2,
                                                                      h_norm=y2,
                                                                      image_width=image_width,
                                                                      image_height=image_height,
                                                                      run_id=run_id)

                                # Add the instance to the session
                                session.add(detection_info)

                                # Create a new instance of the ImageProcessingStatus model
                                image_processing_status = ImageProcessingStatus(image_filename=image_filename,
                                                                                image_upload_date=image_upload_date,
                                                                                image_customer_name=customer_name,
                                                                                processing_status='processed')

                                # Merge the instance into the session (updates if already exists)
                                session.merge(image_processing_status)
                    else:
                        LOGGER.debug('Area to blur is 0.')

                folder_path = os.path.dirname(save_path)
                if not os.path.exists(folder_path):
                    os.makedirs(folder_path)

                if not cv2.imwrite(
                        save_path,
                        im_orig[si],
                ):
                    raise Exception(f'Could not write image {os.path.basename(save_path)}')

        if skip_evaluation:
            # Filter and iterate over paths with no detection in current batch
            false_paths = [path for path in image_detections if not image_detections[path]]

            # Process images with no detection
            for false_path in false_paths:
                image_filename, image_upload_date = extract_upload_date(false_path)

                # Create an instance of DetectionInformation
                detection_info = DetectionInformation(image_customer_name=customer_name,
                                                      image_upload_date=image_upload_date,
                                                      image_filename=image_filename,
                                                      has_detection=False,
                                                      class_id=None,
                                                      x_norm=None,
                                                      y_norm=None,
                                                      w_norm=None,
                                                      h_norm=None,
                                                      image_width=None,
                                                      image_height=None,
                                                      run_id=run_id)

                # Create a new instance of the ImageProcessingStatus model
                image_processing_status = ImageProcessingStatus(image_filename=image_filename,
                                                                image_upload_date=image_upload_date,
                                                                image_customer_name=customer_name,
                                                                processing_status='processed')

                # The session will be automatically closed at the end of this block
                with db_config.managed_session() as session:
                    # Add the instance to the session
                    session.add(detection_info)
                    # Merge the instance into the session (updates if already exists)
                    session.merge(image_processing_status)

        # Plot images
        if plots and not skip_evaluation:
            plot_images(im, targets, paths, save_dir / f'{path.stem}_labelled.jpg', names,
                        conf_thres=conf_thres,
                        no_inverted_colors=no_inverted_colors)  # labels
            plot_images(im,
                        output_to_target(preds),
                        paths,
                        save_dir / f'{path.stem}_pred.jpg',
                        names,
                        conf_thres=conf_thres,
                        no_inverted_colors=no_inverted_colors)  # pred

        callbacks.run('on_val_batch_end', batch_i, im, targets, paths, shapes, preds)

    # Compute metrics
    if not skip_evaluation:
        stats = [torch.cat(x, 0).cpu().numpy() for x in zip(*stats)]  # to numpy
        if len(stats) and stats[0].any():
            tp, fp, p, r, f1, ap, ap_class = ap_per_class(*stats, plot=plots, save_dir=save_dir, names=names)
            ap50, ap = ap[:, 0], ap.mean(1)  # AP@0.5, AP@0.5:0.95
            mp, mr, map50, map = p.mean(), r.mean(), ap50.mean(), ap.mean()
        nt = np.bincount(stats[3].astype(int), minlength=nc)  # number of targets per class

        # Print results
        pf = '%22s' + '%11i' * 2 + '%11.3g' * 4  # print format
        LOGGER.info(pf % ('all', seen, nt.sum(), mp, mr, map50, map))
        if nt.sum() == 0:
            LOGGER.warning(f'WARNING ⚠️ no labels found in {task} set, can not compute metrics without labels')

        # Print results per class
        if (verbose or (nc < 50 and not training)) and nc > 1 and len(stats):
            for i, c in enumerate(ap_class):
                LOGGER.info(pf % (names[c], seen, nt[c], p[i], r[i], ap50[i], ap[i]))

    # Print speeds
    t = tuple(x.t / seen * 1E3 for x in dt)  # speeds per image
    if not training:
        shape = (batch_size, 3, imgsz, imgsz)
        LOGGER.info(f'Speed: %.1fms pre-process, %.1fms inference, %.1fms NMS per image at shape {shape}' % t)

    # Plots
    if plots and not skip_evaluation:
        confusion_matrix.plot(save_dir=save_dir, names=list(names.values()))
        callbacks.run('on_val_end', nt, tp, fp, p, r, f1, ap, ap50, ap_class, confusion_matrix)

    # Save JSON
    if save_json and len(jdict):
        anno_json = str(Path('../datasets/coco/annotations/instances_val2017.json'))  # annotations
        pred_json = str(save_dir / 'predictions.json')  # predictions
        LOGGER.info(f'\nEvaluating pycocotools mAP... saving {pred_json}...')
        with open(pred_json, 'w') as f:
            json.dump(jdict, f)

        try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
            check_requirements('pycocotools>=2.0.6')
            from pycocotools.coco import COCO
            from pycocotools.cocoeval import COCOeval

            anno = COCO(anno_json)  # init annotations api
            pred = anno.loadRes(pred_json)  # init predictions api
            eval = COCOeval(anno, pred, 'bbox')
            if is_coco:
                eval.params.imgIds = [int(Path(x).stem) for x in dataloader.dataset.im_files]  # image IDs to evaluate
            eval.evaluate()
            eval.accumulate()
            eval.summarize()
            map, map50 = eval.stats[:2]  # update results (mAP@0.5:0.95, mAP@0.5)
        except Exception as e:
            LOGGER.info(f'pycocotools unable to run: {e}')

    # Return results
    model.float()  # for training
    if not training:
        s = f"\nTotal {len(list(save_dir.glob('labels/*.txt')))} labels found in the folder {save_dir / 'labels'}" if save_txt else ''
        LOGGER.info(f"Results saved to {colorstr('bold', save_dir)}{s}")
    maps = np.zeros(nc) + map
    for i, c in enumerate(ap_class):
        maps[c] = ap[i]
    if skip_evaluation:
        try:
            trained_yolo_model = os.path.split(weights)[-1]
        except Exception as e:
            print(f"Error while getting trained_yolo_model name: {str(e)}")
            trained_yolo_model = ""

        # Perform database operations using the 'session'
        # The session will be automatically closed at the end of this block
        with db_config.managed_session() as session:
            # Create an instance of BatchRunInformation
            batch_info = BatchRunInformation(run_id=run_id,
                                             start_time=start_time,
                                             end_time=get_current_time(),
                                             trained_yolo_model=trained_yolo_model,
                                             success=True,
                                             error_code=None)

            # Add the instance to the session
            session.add(batch_info)

    if skip_evaluation:
        return (mp, mr, map50, map, []), maps, t
    return (mp, mr, map50, map, *(loss.cpu() / len(dataloader)).tolist()), maps, t


def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, default='/container/landing_zone/pano.yaml', help='dataset.yaml path')
    parser.add_argument('--weights',
                        nargs='+',
                        type=str,
                        default='/container/landing_zone/best.pt',
                        help='model path(s)')
    parser.add_argument('--batch-size', type=int, default=4, help='batch size')
    parser.add_argument('--imgsz', '--img', '--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.001, help='confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.6, help='NMS IoU threshold')
    parser.add_argument('--no-inverted-colors', action='store_true', help='Set this to false when plots have negative '
                                                                           'effect.')
    parser.add_argument('--max-det', type=int, default=300, help='maximum detections per image')
    parser.add_argument('--task', default='val', help='train, val, test, speed or study')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--workers', type=int, default=8, help='max dataloader workers (per RANK in DDP mode)')
    parser.add_argument('--single-cls', action='store_true', help='treat as single-class dataset')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--verbose', action='store_true', help='report mAP by class')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--save-hybrid', action='store_true', help='save label+prediction hybrid results to *.txt')
    parser.add_argument('--save-conf', action='store_true', help='save confidences in --save-txt labels')
    parser.add_argument('--save-json', action='store_true', help='save a COCO-JSON results file')
    parser.add_argument('--project', default=ROOT / 'runs/val', help='save to project/name')
    parser.add_argument('--name', default='exp', help='save to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--half', action='store_true', help='use FP16 half-precision inference')
    parser.add_argument('--dnn', action='store_true', help='use OpenCV DNN for ONNX inference')
    parser.add_argument('--tagged-data', action='store_true', help='use tagged validation')
    parser.add_argument('--skip-evaluation', action='store_true', help='ignore code parts for production')
    parser.add_argument('--save-blurred-image', action='store_true', help='save blurred images')
    parser.add_argument('--customer-name',
                        type=str,
                        default='example_customer',
                        help='the customer for which we process the images')
    parser.add_argument('--run-id',
                        type=str,
                        default='default_run_id',
                        help='the run id generated by Azure Machine Learning')
    parser.add_argument('--db-username', type=str, default='', help='database username')
    parser.add_argument('--db-hostname', type=str, default='', help='database hostname')
    parser.add_argument('--db-name', type=str, default='', help='database name')
    parser.add_argument('--start-time', type=str, help='start time of the Azure ML job')
    opt = parser.parse_args()
    opt.data = check_yaml(opt.data)  # check YAML
    opt.save_json |= opt.data.endswith('coco.yaml')
    opt.save_txt |= opt.save_hybrid
    print_args(vars(opt))
    return opt


def main(opt):
    check_requirements(exclude=('tensorboard', 'thop'))
    if opt.task in ('train', 'val', 'test'):  # run normally
        if opt.conf_thres > 0.001:  # https://github.com/ultralytics/yolov5/issues/1466
            LOGGER.info(f'WARNING ⚠️ confidence threshold {opt.conf_thres} > 0.001 produces invalid results')
        if opt.save_hybrid:
            LOGGER.info('WARNING ⚠️ --save-hybrid will return high mAP from hybrid labels, not from predictions alone')
        run(**vars(opt))

    else:
        weights = opt.weights if isinstance(opt.weights, list) else [opt.weights]
        opt.half = torch.cuda.is_available() and opt.device != 'cpu'  # FP16 for fastest results
        if opt.task == 'speed':  # speed benchmarks
            # python val.py --task speed --data coco.yaml --batch 1 --weights yolov5n.pt yolov5s.pt...
            opt.conf_thres, opt.iou_thres, opt.save_json = 0.25, 0.45, False
            for opt.weights in weights:
                run(**vars(opt), plots=False)

        elif opt.task == 'study':  # speed vs mAP benchmarks
            # python val.py --task study --data coco.yaml --iou 0.7 --weights yolov5n.pt yolov5s.pt...
            for opt.weights in weights:
                f = f'study_{Path(opt.data).stem}_{Path(opt.weights).stem}.txt'  # filename to save to
                x, y = list(range(256, 1536 + 128, 128)), []  # x axis (image sizes), y axis
                for opt.imgsz in x:  # img-size
                    LOGGER.info(f'\nRunning {f} --imgsz {opt.imgsz}...')
                    r, _, t = run(**vars(opt), plots=False)
                    y.append(r + t)  # results and times
                np.savetxt(f, y, fmt='%10.4g')  # save
            subprocess.run(['zip', '-r', 'study.zip', 'study_*.txt'])
            plot_val_study(x=x)  # plot
        else:
            raise NotImplementedError(f'--task {opt.task} not in ("train", "val", "test", "speed", "study")')


if __name__ == '__main__':
    opt = parse_opt()
    main(opt)
