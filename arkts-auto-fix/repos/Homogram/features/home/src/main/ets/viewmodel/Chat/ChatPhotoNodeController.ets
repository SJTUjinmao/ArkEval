// CustomComponent.ets
// 自定义占位节点，跨容器迁移能力
import { BuilderNode, display, FrameNode, NodeController } from '@kit.ArkUI';
import { Logger } from '@ohos/base';
import { HomeConstants } from '../../constants/HomeConstants';
import { getOrDownloadProfilePhoto, getThumbProfilePhotoPixelMap } from '../../utils';
import { HashMap } from '@kit.ArkTS';
import { ImageKnifeComponent, ImageKnifeOption } from '@ohos/imageknife';
import { image } from '@kit.ImageKit';
import { resourceManager } from '@kit.LocalizationKit';

class ChatPhotoData {
  chatId: number | undefined = undefined;
  path: string | undefined = undefined;
  thumb: image.PixelMap | undefined = undefined;
  myThis?: Object | undefined = undefined;
}

/**
 * 生成圆形渐变背景字母图片
 * @param size 图片尺寸（宽高相等）
 * @param letter 要显示的字母
 * @param startColor 渐变起始颜色（RGB格式，例如：'#FF0000'）
 * @param endColor 渐变结束颜色（RGB格式，例如：'#0000FF'）
 * @returns Promise<PixelMap>
 */
function createCircleLetterPixelMap(
  letter: string,
  myThis?: Object | undefined
): PixelMap {
  const context: Context = getContext(myThis);
  const resourceMgr : resourceManager.ResourceManager = context.resourceManager;

  let data = resourceMgr.getMediaContentSync($r('app.media.fallback_vibrant_orange_gradient'));
  const imageSource = image.createImageSource(data.buffer.slice(data.byteOffset, data.byteLength + data.byteOffset));
  const imageOpts: image.DecodingOptions = {
    editable: true,
    desiredSize: {
      height: HomeConstants.PROFILE_PHOTO_SIZE,
      width: HomeConstants.PROFILE_PHOTO_SIZE
    }
  };
  const pixelMap = imageSource.createPixelMapSync(imageOpts);

  let offScreenCanvas = new OffscreenCanvas(HomeConstants.PROFILE_PHOTO_SIZE, HomeConstants.PROFILE_PHOTO_SIZE);
  let offScreenContext = offScreenCanvas.getContext("2d");
  const imageScale = 1.0;
  offScreenContext.drawImage(pixelMap, 0, 0, offScreenCanvas.width, offScreenCanvas.height);
  offScreenContext.textAlign = 'center';
  offScreenContext.textBaseline = 'middle';
  offScreenContext.fillStyle = '#00000000';
  offScreenContext.font = 32 * imageScale + "vp";
  offScreenContext.fillText(letter.toUpperCase(), offScreenCanvas.width - 24 * imageScale,
    offScreenCanvas.height - 16 * imageScale);

  return offScreenContext.getPixelMap(0, 0, offScreenCanvas.width, offScreenCanvas.height);
}

// async function customGetImage(context: Context, src: string | PixelMap | Resource): Promise<ArrayBuffer | undefined> {
//   let pixelMap = createCircleLetterPixelMap("T", undefined);
//   let info = pixelMap.getImageInfoSync();
//   let size = info.size.width * info.size.height * 4 * 2;
//   let buffer = new ArrayBuffer(size);
//   pixelMap.readPixelsToBuffer(buffer);
//   return buffer;
// }

@Builder
async function ChatPhotoBuilder(data: ChatPhotoData, myThis?: Object | undefined) {
  ImageKnifeComponent({
    imageKnifeOption: new ImageKnifeOption({
      loadSrc: "file://" + data.path,
      placeholderSrc: data.thumb,
      // errorholderSrc: createCircleLetterPixelMap("T", myThis),
      errorholderSrc: $r('app.media.fallback_vibrant_orange_gradient'),
      objectFit: ImageFit.Auto,
      border: {
        radius: 50
      },
      // customGetImage: customGetImage,
    })
  })
    .width(HomeConstants.PROFILE_PHOTO_SIZE)
    .height(HomeConstants.PROFILE_PHOTO_SIZE)
    .id(data.chatId?.toString())
}

export class ChatPhotoNodeController extends NodeController {
  private chatPhotoNode: BuilderNode<ChatPhotoData[]> | null = null;
  private isRemove: boolean = false;
  private callback: Function | undefined = undefined
  private data: ChatPhotoData | null = null

  makeNode(uiContext: UIContext): FrameNode | null {
    if (this.isRemove == true) {
      return null;
    }
    if (this.chatPhotoNode != null) {
      return this.chatPhotoNode.getFrameNode();
    }

    return null;
  }

  init(uiContext: UIContext, chatId: number | undefined, path: string | undefined, thumb: image.PixelMap | undefined) {
    if (this.chatPhotoNode != null) {
      return;
    }
    this.chatPhotoNode = new BuilderNode(uiContext);
    this.data = {
      chatId: chatId,
      path: path,
      thumb: thumb,
      myThis: this,
    };
    this.chatPhotoNode.build(wrapBuilder<ChatPhotoData[]>(ChatPhotoBuilder), this.data);
  }

  update(chatId: number | undefined, path: string | undefined, thumb: image.PixelMap | undefined) {
    if (this.chatPhotoNode != null) {
      this.data = {
        chatId: chatId,
        path: path,
        thumb: thumb
      };
      this.chatPhotoNode.update(this.data);
    }
  }

  setCallback(callback: Function | undefined) {
    this.callback = callback
  }

  getNode(): BuilderNode<[ChatPhotoData]> | null {
    return this.chatPhotoNode;
  }

  setNode(node: BuilderNode<[ChatPhotoData]> | null) {
    this.chatPhotoNode = node;
    this.rebuild();
  }

  onRemove() {
    this.isRemove = true;
    this.rebuild();
    this.isRemove = false;
  }
}

let chatPhotoNodeMap: HashMap<number, ChatPhotoNodeController> = new HashMap();

export const createChatPhotoNode =
  (uiContext: UIContext, chatId: number | undefined, path: string | undefined, thumb: image.PixelMap | undefined): ChatPhotoNodeController | undefined => {
    let node = new ChatPhotoNodeController();
    node.init(uiContext, chatId, path, thumb);
    chatPhotoNodeMap.set(chatId, node);
    return node;
  }

export const getChatPhotoNode = (chatId: number | undefined): ChatPhotoNodeController | undefined => {
  return chatPhotoNodeMap.get(chatId);
}

export const deleteChatPhotoNode = (chatId: number | undefined) => {
  chatPhotoNodeMap.remove(chatId);
}