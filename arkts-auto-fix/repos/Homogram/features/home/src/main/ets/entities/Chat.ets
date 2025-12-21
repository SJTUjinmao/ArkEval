import sql from "@liushengyi/smartdb"
import { NativeChat, ChatType } from 'libhomogrape.so';
import { IComparable } from '@ohos/base'

export class Chat implements NativeChat, IComparable<Chat> {
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatId: number;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatType: ChatType;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  name: string;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  pinned: boolean;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  lastMessageSenderName: string;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  lastMessageId: number;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  lastMessageText: string;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  lastMessageTimestamp: number;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  megagroup: boolean;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  forum: boolean;

  constructor(chatId: number, chatType: ChatType, name: string, pinned: boolean, lastMessageId: number,
    lastMessageSenderName: string,
    lastMessageText: string, lastMessageTimestamp: number, megagroup: boolean, forum: boolean) {
    this.chatId = chatId;
    this.chatType = chatType;
    this.name = name;
    this.pinned = pinned;
    this.lastMessageId = lastMessageId;
    this.lastMessageSenderName = lastMessageSenderName;
    this.lastMessageText = lastMessageText;
    this.lastMessageTimestamp = lastMessageTimestamp;
    this.megagroup = megagroup;
    this.forum = forum;
  }

  public static fromNative(nativeChat: NativeChat): Chat {
    return new Chat(nativeChat.chatId, nativeChat.chatType, nativeChat.name, nativeChat.pinned,
      nativeChat.lastMessageId, nativeChat.lastMessageSenderName, nativeChat.lastMessageText,
      nativeChat.lastMessageTimestamp, nativeChat.megagroup,
      nativeChat.forum);
  }

  public toNative(): NativeChat {
    return {
      chatId: this.chatId,
      chatType: this.chatType,
      name: this.name,
      pinned: this.pinned,
      lastMessageId: this.lastMessageId,
      lastMessageSenderName: this.lastMessageSenderName,
      lastMessageText: this.lastMessageText,
      lastMessageTimestamp: this.lastMessageTimestamp,
      megagroup: this.megagroup,
      forum: this.forum
    };
  }

  compareTo(other: Chat): number {
    return this.lastMessageTimestamp - other.lastMessageTimestamp;
  }
}