
import torch
import torchvision
from torch import nn
from torch.nn import functional as F
from d2l import torch as d2l


def cls_predictor(num_inputs, num_anchors, num_classes):
    return nn.Conv2d(num_inputs, num_anchors * (num_classes + 1),
                     kernel_size=3, padding=1)

def bbox_predictor(num_inputs, num_anchors):
    return nn.Conv2d(num_inputs, num_anchors * 4, kernel_size=3, padding=1)

def forward(x, block):
    return block(x)

def flatten_pred(pred):
    return torch.flatten(pred.permute(0, 2, 3, 1), start_dim=1)

def concat_preds(preds):
    return torch.cat([flatten_pred(p) for p in preds], dim=1)

def down_sample_blk(in_channels, out_channels):
    blk = []
    for _ in range(2):
        blk.append(nn.Conv2d(in_channels, out_channels,
                             kernel_size=3, padding=1))
        blk.append(nn.BatchNorm2d(out_channels))
        blk.append(nn.ReLU())
        in_channels = out_channels
    blk.append(nn.MaxPool2d(2))
    return nn.Sequential(*blk)

def base_net():
    blk = []
    num_filters = [3, 16, 32, 64]
    for i in range(len(num_filters) - 1):
        blk.append(down_sample_blk(num_filters[i], num_filters[i+1]))
    return nn.Sequential(*blk)

def get_blk(i):
    if i == 0:
        blk = base_net()
    elif i == 1:
        blk = down_sample_blk(64, 128)
    elif i == 4:
        blk = nn.AdaptiveMaxPool2d((1,1))
    else:
        blk = down_sample_blk(128, 128)
    return blk

def blk_forward(X, blk, size, ratio, cls_predictor, bbox_predictor):
    Y = blk(X)
    anchors = d2l.multibox_prior(Y, sizes=size, ratios=ratio)
    cls_preds = cls_predictor(Y)
    bbox_preds = bbox_predictor(Y)
    return (Y, anchors, cls_preds, bbox_preds)

class TinySSD(nn.Module):
    def __init__(self, num_classes, num_anchors,sizes,ratios,**kwargs):
        super(TinySSD, self).__init__(**kwargs)
        self.num_classes = num_classes
        self.num_anchors = num_anchors
        self.sizes = sizes
        self.ratios = ratios
        idx_to_in_channels = [64, 128, 128, 128, 128]
        for i in range(5):
            # 即赋值语句self.blk_i=get_blk(i)
            setattr(self, f'blk_{i}', get_blk(i))
            setattr(self, f'cls_{i}', cls_predictor(idx_to_in_channels[i],
                                                    num_anchors, num_classes))
            setattr(self, f'bbox_{i}', bbox_predictor(idx_to_in_channels[i],
                                                      num_anchors))

    def forward(self, X):
        anchors, cls_preds, bbox_preds = [None] * 5, [None] * 5, [None] * 5
        for i in range(5):
            # getattr(self,'blk_%d'%i)即访问self.blk_i
            X, anchors[i], cls_preds[i], bbox_preds[i] = blk_forward(
                X, getattr(self, f'blk_{i}'), self.sizes[i], self.ratios[i],
                getattr(self, f'cls_{i}'), getattr(self, f'bbox_{i}'))
        anchors = torch.cat(anchors, dim=1)
        cls_preds = concat_preds(cls_preds)
        cls_preds = cls_preds.reshape(
            cls_preds.shape[0], -1, self.num_classes + 1)
        bbox_preds = concat_preds(bbox_preds)
        return anchors, cls_preds, bbox_preds


def calc_loss(cls_preds, cls_labels, bbox_preds, bbox_labels, bbox_masks):
    cls_loss = nn.CrossEntropyLoss(reduction='none')
    bbox_loss = nn.L1Loss(reduction='none')

    batch_size, num_classes = cls_preds.shape[0], cls_preds.shape[2]
    cls = cls_loss(cls_preds.reshape(-1, num_classes),
                   cls_labels.reshape(-1)).reshape(batch_size, -1).mean(dim=1)
    bbox = bbox_loss(bbox_preds * bbox_masks,
                     bbox_labels * bbox_masks).mean(dim=1)
    return cls + bbox

def cls_eval(cls_preds, cls_labels):
    # 由于类别预测结果放在最后一维，argmax需要指定最后一维。
    return float((cls_preds.argmax(dim=-1).type(
        cls_labels.dtype) == cls_labels).sum())

def bbox_eval(bbox_preds, bbox_labels, bbox_masks):
    return float((torch.abs((bbox_labels - bbox_preds) * bbox_masks)).sum())

def train():
    num_epochs, timer = 20, d2l.Timer()
    animator = d2l.Animator(xlabel='epoch', xlim=[1, num_epochs],
                            legend=['class error', 'bbox mae'])
    batch_size = 32
    train_iter, _ = d2l.load_data_bananas(batch_size)

    sizes = [[0.2, 0.272], [0.37, 0.447], [0.54, 0.619], [0.71, 0.79],
             [0.88, 0.961]]
    ratios = [[1, 2, 0.5]] * 5
    num_anchors = len(sizes[0]) + len(ratios[0]) - 1

    device, net = d2l.try_gpu(), TinySSD(num_classes=1,sizes=sizes,ratios=ratios,num_anchors =num_anchors )
    trainer = torch.optim.SGD(net.parameters(), lr=0.2, weight_decay=5e-4)

    net = net.to(device)
    for epoch in range(num_epochs):
        # 训练精确度的和，训练精确度的和中的示例数
        # 绝对误差的和，绝对误差的和中的示例数
        metric = d2l.Accumulator(4)
        net.train()
        for features, target in train_iter:
            timer.start()
            trainer.zero_grad()
            X, Y = features.to(device), target.to(device)
            # 生成多尺度的锚框，为每个锚框预测类别和偏移量
            anchors, cls_preds, bbox_preds = net(X)
            # 为每个锚框标注类别和偏移量
            bbox_labels, bbox_masks, cls_labels = d2l.multibox_target(anchors, Y)
            # 根据类别和偏移量的预测和标注值计算损失函数
            l = calc_loss(cls_preds, cls_labels, bbox_preds, bbox_labels,
                          bbox_masks)
            l.mean().backward()
            trainer.step()
            metric.add(cls_eval(cls_preds, cls_labels), cls_labels.numel(),
                       bbox_eval(bbox_preds, bbox_labels, bbox_masks),
                       bbox_labels.numel())
        cls_err, bbox_mae = 1 - metric[0] / metric[1], metric[2] / metric[3]
        animator.add(epoch + 1, (cls_err, bbox_mae))
    print(f'class err {cls_err:.2e}, bbox mae {bbox_mae:.2e}')
    print(f'{len(train_iter.dataset) / timer.stop():.1f} examples/sec on '
          f'{str(device)}')

    #保存模型

    PATH = './sdd.pkl'

    saveModule(net,trainer)



def saveModule(model,optimizer):
    print(model.state_dict().keys())  # 输出模型参数名称

    # # 保存模型参数到路径"./data/model_parameter.pkl"
    # torch.save(model.state_dict(), "./model_parameter.pkl")

    torch.save({
        # 'epoch': epoch,
        'model_dict': model.state_dict(),
        'optimizer_dict': optimizer.state_dict(),
        # 'scheduler_dict': scheduler.state_dict(),
        # 'loss': loss,

    }, "./model_parameter.pkl")



def readModule(**path):
    sizes = [[0.2, 0.272], [0.37, 0.447], [0.54, 0.619], [0.71, 0.79],
             [0.88, 0.961]]
    ratios = [[1, 2, 0.5]] * 5
    num_anchors = len(sizes[0]) + len(ratios[0]) - 1


    model = TinySSD(num_classes=1, sizes=sizes, ratios=ratios, num_anchors=num_anchors)  # 调用模型Model
    # model.load_state_dict(torch.load("./model_parameter.pkl"))  # 加载模型参数
    # new_model.forward(input)  # 进行使用



    # 加载
    checkpoint = torch.load("./model_parameter.pkl")
    model.load_state_dict(checkpoint['model_dict'])
    # trainer.load_state_dict(checkpoint['optimizer_dict'])
    # epoch = checkpoint['epoch']
    #
    # loss = checkpoint['loss']

    model.eval()



    return model

def validation(device):
    print("start")
    # 加载完整模型
    net = readModule()

    X = torchvision.io.read_image('../img/8.png').unsqueeze(0).float()
    img = X.squeeze(0).permute(1, 2, 0).long()
    output = predict(net,X,device)
    display(img, output.cpu(), threshold=0.01)
    print("end")

def predict(net,X,device):

    anchors, cls_preds, bbox_preds = net(X.to(device))
    cls_probs = F.softmax(cls_preds, dim=2).permute(0, 2, 1)
    print(cls_preds)
    print( )
    print(bbox_preds)
    output = d2l.multibox_detection(cls_probs, bbox_preds, anchors)
    idx = [i for i, row in enumerate(output[0]) if row[0] != -1]
    return output[0, idx]

def display(img, output, threshold):
    d2l.set_figsize((5, 5))
    fig = d2l.plt.imshow(img)
    for row in output:
        print(1)
        score = float(row[1])
        if score < threshold:
            continue
        h, w = img.shape[0:2]
        bbox = [row[2:6] * torch.tensor((w, h, w, h), device=row.device)]
        d2l.show_bboxes(fig.axes, bbox, '%.2f' % score, 'w')
        d2l.plt.show()

# train()
validation(d2l.try_gpu())