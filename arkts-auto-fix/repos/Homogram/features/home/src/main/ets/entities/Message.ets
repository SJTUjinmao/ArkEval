import sql from "@liushengyi/smartdb"
import { MediaType, NativeMessage } from 'libhomogrape.so'
import { IComparable } from '@ohos/base'

export class Message implements NativeMessage, IComparable<Message> {
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatId: number;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  messageId: number;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  outgoing: boolean;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  pinned: boolean;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  senderId: number;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  senderName: string;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  timestamp: number;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  text: string;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  mediaType: MediaType
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  editTimestamp?: number
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  groupedId?: number
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  replyToMessageId?: number

  constructor(chatId: number, messageId: number, outgoing: boolean, pinned: boolean, senderId: number,
    senderName: string, timestamp: number, text: string, mediaType: MediaType, editTimestamp?: number,
    groupedId?: number, replyToMessageId?: number) {
    this.chatId = chatId;
    this.messageId = messageId;
    this.outgoing = outgoing;
    this.pinned = pinned;
    this.senderId = senderId;
    this.senderName = senderName;
    this.timestamp = timestamp;
    this.text = text;
    this.mediaType = mediaType;
    this.editTimestamp = editTimestamp || -1;
    this.groupedId = groupedId || -1;
    this.replyToMessageId = replyToMessageId || -1;
  }

  public static fromNative(nativeMessage: NativeMessage): Message {
    return new Message(nativeMessage.chatId, nativeMessage.messageId, nativeMessage.outgoing, nativeMessage.pinned,
      nativeMessage.senderId, nativeMessage.senderName, nativeMessage.timestamp, nativeMessage.text,
      nativeMessage.mediaType, nativeMessage.editTimestamp, nativeMessage.groupedId, nativeMessage.replyToMessageId);
  }

  compareTo(other: Message): number {
    return other.messageId - this.messageId;
  }
}