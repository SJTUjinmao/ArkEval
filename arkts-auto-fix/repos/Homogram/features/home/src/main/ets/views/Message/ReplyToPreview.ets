import { HashMap } from '@kit.ArkTS';
import { BaseConstants, Logger } from '@ohos/base';
import { downloadMediaFromMessage, MediaType } from 'libhomogrape.so';
import { MessageDataSource } from '../../viewmodel/Message/MessageDataSource';
import { fileIo as fs, ReadOptions } from '@kit.CoreFileKit';
import { HomeConstants } from '../../constants/HomeConstants';

@Component
export struct ReplyToPreview {
  @Prop replyToMessageId: number;
  @Consume currentChatId: string;
  @Consume messagesOfChatMap: HashMap<string, MessageDataSource>;
  @State mediaPath: string = "";
  private message = this.messagesOfChatMap.get(this.currentChatId)?.getMessageById(this.replyToMessageId);

  build() {

    Row() {
      Blank()
        .color(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_DIVIDER_COLOR)
        .width(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_DIVIDER_WIDTH)
        .margin({ left: -2, right: 4, })
      if (this.message?.mediaType === MediaType.Photo) {
        Image("file://" + this.mediaPath)
          .width(30)
          .height(30)
          .objectFit(ImageFit.Auto)
          .borderRadius(8)
          .margin({ right: 4, })
          .onAppear(() => {
            this.mediaPath =
              BaseConstants.BASE_DIR + this.message?.chatId.toString() + "/" + this.message?.messageId.toString() +
                ".jpg";
            if (!fs.accessSync(this.mediaPath)) {
              Logger.debug("ReplyToPreview ImagePreview onAppear: media not exists, downloading media");
              downloadMediaFromMessage(this.message?.chatId, this.message?.messageId)
                .then((path) => {
                  Logger.debug("ReplyToPreview ImagePreview onAppear: media downloaded: " + path);
                  this.mediaPath = path;
                })
                .catch((error: Error) => {
                  Logger.debug("ReplyToPreview ImagePreview onAppear Failed to download media: " + error.message);
                });
            } else {
              Logger.debug("ReplyToPreview ImagePreview onAppear: media exists");
            }
          })
      }
      Column() {
        Text(this.message?.senderName)
          .fontSize(12)
          .fontWeight(FontWeight.Bold)
          .fontColor(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_DIVIDER_COLOR)
          .maxLines(1)
          .textOverflow({ overflow: TextOverflow.Ellipsis })
        Text(this.message?.text === "" ? "Photo" : this.message?.text)
          .fontSize(10)
          .maxLines(1)
          .textOverflow({ overflow: TextOverflow.Ellipsis })
      }
      .alignItems(HorizontalAlign.Start)

      // Divider()
      //   .vertical(true)
      //   .strokeWidth(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_DIVIDER_WIDTH)
      //   .color(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_DIVIDER_COLOR)
      //   .borderRadius(8)
      //   .outlineRadius(8)
      //   .height(40)
    }
    .alignItems(VerticalAlign.Center)
    .padding(8)
    .backgroundColor(HomeConstants.MESSAGE_REPLY_TO_PREVIEW_BACKGROUND_COLOR)
    .borderRadius(8)
    .height(40)
  }
}
