import torch
import torch.nn as nn
from torchvision.ops import nms
import numpy as np

"""
对yolo的结果进行解码
"""
class DecodeBox():
    def __init__(self, anchors, num_classes, input_shape, anchors_mask = [[6,7,8], [3,4,5], [0,1,2]]):
        super(DecodeBox, self).__init__()
        self.anchors        = anchors           # 先验框
        self.num_classes    = num_classes
        self.bbox_attrs     = 5 + num_classes
        self.input_shape    = input_shape       # 原始图像大小
        #-----------------------------------------------------------#
        #   13x13的特征层对应的anchor是[116,90],[156,198],[373,326]
        #   26x26的特征层对应的anchor是[30,61],[62,45],[59,119]
        #   52x52的特征层对应的anchor是[10,13],[16,30],[33,23]
        #-----------------------------------------------------------#
        self.anchors_mask   = anchors_mask

    def decode_box(self, inputs):
        """
        inputs: out0, out1, out2
                13    26    52
        """
        outputs = []
        for i, input in enumerate(inputs):
            #-----------------------------------------------#
            #   输入的input一共有三个，他们的shape分别是
            #   b, 75, 13, 13
            #   b, 75, 26, 26
            #   b, 75, 52, 52
            #-----------------------------------------------#
            # 卷积后图片数,高宽
            batch_size      = input.size(0)
            input_height    = input.size(2)
            input_width     = input.size(3)

            #-----------------------------------------------#
            #   调整步长,一次找多少像素,每个特征点对应原图多少个像素
            #   输入为416x416时
            #   stride_h = stride_w = 416/13=32、416/26=16、416/52=8
            #-----------------------------------------------#
            stride_h = self.input_shape[0] / input_height
            stride_w = self.input_shape[1] / input_width
            #-------------------------------------------------#
            #   调整anchor的宽高到相对于特征层的大小(除以步长即可)
            #   此时获得的scaled_anchors大小是相对于特征层的
            #-------------------------------------------------#
            scaled_anchors = [(anchor_width / stride_w, anchor_height / stride_h) for anchor_width, anchor_height in self.anchors[self.anchors_mask[i]]]

            #-----------------------------------------------#
            #   reshape: b, 75, 13, 13 => b, 3, 25, 13, 13
            #   输入的input一共有三个，他们的shape分别是
            #   b, 3, 25, 13, 13
            #   b, 3, 25, 26, 26
            #   b, 3, 25, 52, 52
            #   再将框(2维)的内容变换到最后维度
            #   b, 3, 13, 13, 25  25=4+1+20 4代表x_offset, y_offset, h, w 1代表置信度
            #-----------------------------------------------#
            prediction = input.view(batch_size, len(self.anchors_mask[i]),
                                    self.bbox_attrs, input_height, input_width).permute(0, 1, 3, 4, 2).contiguous()

            #-----------------------------------------------#
            #   先验框的中心位置的调整参数
            #   sigmoid固定到0~1之间,每个框的中心点是按照分隔后的最近的左上角点,就是物体位置由网格左上角来负责预测
            #   调整到0~1之间就是坐标点的移动位置在右下角的方框内
            #-----------------------------------------------#
            x = torch.sigmoid(prediction[..., 0])
            y = torch.sigmoid(prediction[..., 1])
            #-----------------------------------------------#
            #   先验框的宽高调整参数
            #-----------------------------------------------#
            w = prediction[..., 2]
            h = prediction[..., 3]
            #-----------------------------------------------#
            #   获得置信度，是否有物体
            #-----------------------------------------------#
            conf        = torch.sigmoid(prediction[..., 4])
            #-----------------------------------------------#
            #   种类置信度
            #   0~1之间,表示可能性
            #-----------------------------------------------#
            pred_cls    = torch.sigmoid(prediction[..., 5:])

            FloatTensor = torch.cuda.FloatTensor if x.is_cuda else torch.FloatTensor
            LongTensor  = torch.cuda.LongTensor if x.is_cuda else torch.LongTensor

            """
            下面要生成默认的先验框,然后使用预测结果进行位置调整
            """
            #----------------------------------------------------------#
            #   生成先验框网格，先验框中心，网格左上角
            #   b,3,13,13 代表13x13的网格上每个网格都有3个先验框
            #----------------------------------------------------------#
            grid_x = torch.linspace(0, input_width - 1, input_width).repeat(input_height, 1).repeat(
                batch_size * len(self.anchors_mask[i]), 1, 1).view(x.shape).type(FloatTensor)
            grid_y = torch.linspace(0, input_height - 1, input_height).repeat(input_width, 1).t().repeat(
                batch_size * len(self.anchors_mask[i]), 1, 1).view(y.shape).type(FloatTensor)

            #----------------------------------------------------------#
            #   按照先验框网格格式生成先验框的宽高
            #   b,3,13,13
            #----------------------------------------------------------#
            anchor_w = FloatTensor(scaled_anchors).index_select(1, LongTensor([0]))
            anchor_h = FloatTensor(scaled_anchors).index_select(1, LongTensor([1]))
            anchor_w = anchor_w.repeat(batch_size, 1).repeat(1, 1, input_height * input_width).view(w.shape)
            anchor_h = anchor_h.repeat(batch_size, 1).repeat(1, 1, input_height * input_width).view(h.shape)

            """
            利用预测值对默认先验框进行调整
            """
            #----------------------------------------------------------#
            #   利用预测结果对默认先验框进行调整
            #   首先调整先验框的中心，从先验框中心向右下角偏移
            #   再调整先验框的宽高。
            #----------------------------------------------------------#
            pred_boxes          = FloatTensor(prediction[..., :4].shape)
            pred_boxes[..., 0]  = x.data + grid_x   # x,y是预测中心, grid_x,grid_y是默认框的位置,相加就能获取位置,将左上角向右下移动
            pred_boxes[..., 1]  = y.data + grid_y
            pred_boxes[..., 2]  = torch.exp(w.data) * anchor_w  # w,h是预测宽高,anchor_w,anchor_h是默认框的宽高
            pred_boxes[..., 3]  = torch.exp(h.data) * anchor_h  # 指数乘以原值

            #----------------------------------------------------------#
            #   将xywh归一化成小数的形式,目前的移动是相对于13x13的调整,除以13之后就相对于宽高归一化了(也相对于原图归一化了,这里的13相对于原图就是416)
            #   前面先验框宽高除以了32,这里再除以13,就相当于除以了416,相对对原图归一化
            #   input_width=input_height=13,26,52
            #----------------------------------------------------------#
            _scale = torch.Tensor([input_width, input_height, input_width, input_height]).type(FloatTensor)
            #----------------------------------------------------------#
            #   output: [b, 3*13*13, 85]
            #           [b, 3*26*26, 85]
            #           [b, 3*52*52, 85]
            #   85: x y w h 先验框置信度 种类置信度
            #----------------------------------------------------------#
            output = torch.cat((pred_boxes.view(batch_size, -1, 4) / _scale,
                                conf.view(batch_size, -1, 1), pred_cls.view(batch_size, -1, self.num_classes)), -1)
            outputs.append(output.data)
        return outputs

    """
    去除图片灰条
    """
    def yolo_correct_boxes(self, box_xy, box_wh, input_shape, image_shape, letterbox_image):
        #-----------------------------------------------------------------#
        #   把y轴放前面是因为方便预测框和图像的宽高进行相乘
        #-----------------------------------------------------------------#
        box_yx = box_xy[..., ::-1]
        box_hw = box_wh[..., ::-1]
        input_shape = np.array(input_shape)
        image_shape = np.array(image_shape)

        if letterbox_image:
            #-----------------------------------------------------------------#
            #   这里求出来的offset是图像有效区域相对于图像左上角的偏移情况
            #   new_shape指的是宽高缩放情况
            #-----------------------------------------------------------------#
            new_shape = np.round(image_shape * np.min(input_shape/image_shape))
            offset  = (input_shape - new_shape)/2./input_shape
            scale   = input_shape/new_shape

            box_yx  = (box_yx - offset) * scale
            box_hw *= scale

        box_mins    = box_yx - (box_hw / 2.)
        box_maxes   = box_yx + (box_hw / 2.)
        boxes  = np.concatenate([box_mins[..., 0:1], box_mins[..., 1:2], box_maxes[..., 0:1], box_maxes[..., 1:2]], axis=-1)
        boxes *= np.concatenate([image_shape, image_shape], axis=-1)
        return boxes

    """
    非极大抑制,筛选出一定区域内属于同一种类得分最大的框
    """
    def non_max_suppression(self, prediction, num_classes, input_shape, image_shape, letterbox_image, conf_thres=0.5, nms_thres=0.4):
        #----------------------------------------------------------#
        #   将预测结果的格式转换成左上角右下角的格式,坐标宽高相对原图是归一化的
        #   prediction: [batch_size, num_anchors, 25]
        #----------------------------------------------------------#
        box_corner          = prediction.new(prediction.shape)
        box_corner[:, :, 0] = prediction[:, :, 0] - prediction[:, :, 2] / 2     # x - 1/2 w = x1
        box_corner[:, :, 1] = prediction[:, :, 1] - prediction[:, :, 3] / 2     # y - 1/2 h = y1
        box_corner[:, :, 2] = prediction[:, :, 0] + prediction[:, :, 2] / 2     # x + 1/2 w = x2
        box_corner[:, :, 3] = prediction[:, :, 1] + prediction[:, :, 3] / 2     # y + 1/2 h = y2
        prediction[:, :, :4] = box_corner[:, :, :4]                             # 替换前4个数据换成左上角右下角的格式

        output = [None for _ in range(len(prediction))]

        #----------------------------------------------------------#
        #   循环图片,一张图片一次
        #----------------------------------------------------------#
        for i, image_pred in enumerate(prediction):
            #----------------------------------------------------------#
            #   image_pred: [num_boxes, 1+4+num_classes]
            #   image_pred[:, 5:5 + num_classes] 取出分类信息
            #   对种类预测部分取max。
            #   class_conf  [num_anchors, 1]    种类置信度
            #   class_pred  [num_anchors, 1]    种类
            #----------------------------------------------------------#
            class_conf, class_pred = torch.max(image_pred[:, 5:5 + num_classes], dim=1, keepdim=True)

            #----------------------------------------------------------#
            #   利用种类置信度进行第一轮筛选,是否大于门限,返回0/1
            #   image_pred[:, 4] * class_conf[:, 0]  是否包含物体 * 置信度 得到最后的置信度
            #----------------------------------------------------------#
            conf_mask = (image_pred[:, 4] * class_conf [:, 0] >= conf_thres).squeeze()

            #----------------------------------------------------------#
            #   根据置信度进行预测结果的筛选,使用0/1筛选
            #----------------------------------------------------------#
            image_pred = image_pred[conf_mask]  # 网络预测结果
            class_conf = class_conf[conf_mask]  # 种类置信度
            class_pred = class_pred[conf_mask]  # 种类
            if not image_pred.size(0):
                continue

            #-------------------------------------------------------------------------#
            #   堆叠位置参数,是否有物体,种类置信度,种类
            #   detections  [num_anchors, 7]
            #   7的内容为：x1, y1, x2, y2, obj_conf(是否包含物体置信度), class_conf(种类置信度), class_pred(种类预测值)
            #-------------------------------------------------------------------------#
            detections = torch.cat((image_pred[:, :5], class_conf.float(), class_pred.float()), 1)

            #------------------------------------------#
            #   获得预测结果中包含的所有种类
            #------------------------------------------#
            unique_labels = detections[:, -1].cpu().unique()    # 种类.unique减少后面的循环

            if prediction.is_cuda:
                unique_labels = unique_labels.cuda()
                detections = detections.cuda()

            # 循环所有预测的种类
            for c in unique_labels:
                #------------------------------------------#
                #   获得某一类得分筛选后全部的预测结果
                #------------------------------------------#
                detections_class = detections[detections[:, -1] == c]   # detections[:, -1] == c 循环获得类别

                #------------------------------------------#
                #   使用官方自带的非极大抑制会速度更快一些！
                #------------------------------------------#
                keep = nms(
                    detections_class[:, :4],                            # 坐标,是相对于原始宽高归一化的坐标
                    detections_class[:, 4] * detections_class[:, 5],    # 先验框置信度 * 种类置信度 结果是1维数据
                    nms_thres                                           # 门限
                )
                max_detections = detections_class[keep]

                # # 按照存在物体的置信度排序
                # _, conf_sort_index = torch.sort(detections_class[:, 4]*detections_class[:, 5], descending=True)
                # # 按照排序好的进行重新取值
                # detections_class = detections_class[conf_sort_index]
                # # 进行非极大抑制
                # max_detections = []
                # while detections_class.size(0):
                #     # 取出这一类置信度最高的，一步一步往下判断，判断重合程度是否大于nms_thres，如果是则去除掉
                #     max_detections.append(detections_class[0].unsqueeze(0))
                #     if len(detections_class) == 1:
                #         break
                #     # 计算重合程度
                #     ious = bbox_iou(max_detections[-1], detections_class[1:])
                #     # 如果IoU小于阈值就保留,说明重合程度低,否则就丢弃
                #     detections_class = detections_class[1:][ious < nms_thres]     # [1:] 相当于删除第一个,每一次都减少
                # # 堆叠获得的框
                # max_detections = torch.cat(max_detections).data

                # Add max detections to outputs
                output[i] = max_detections if output[i] is None else torch.cat((output[i], max_detections))

            # 去除图片灰条
            if output[i] is not None:
                output[i]           = output[i].cpu().numpy()
                box_xy, box_wh      = (output[i][:, 0:2] + output[i][:, 2:4])/2, output[i][:, 2:4] - output[i][:, 0:2]
                output[i][:, :4]    = self.yolo_correct_boxes(box_xy, box_wh, input_shape, image_shape, letterbox_image)
        #-----------------------------------------------#
        #   results = [[
        #               [x1, y1, x2, y2, obj_conf(是否包含物体置信度), class_conf(种类置信度), class_pred(种类预测值)],
        #               ...
        #           ]]
        #-----------------------------------------------#
        return output
