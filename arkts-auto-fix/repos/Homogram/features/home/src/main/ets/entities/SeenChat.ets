import sql from '@liushengyi/smartdb';
import { Base64 } from '@ohos/base64';
import { NativeSeenChat, ChatType } from 'libhomogrape.so';

export class SeenChat {
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatId: number;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatType: ChatType;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  packedChat: string;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  isContact: boolean;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  isMutualContact: boolean;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  phone?: string | undefined;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  username?: string | undefined;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  photoThumb?: string | undefined;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  fullName: string;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  firstName: string;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  lastName?: string | undefined;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  bio?: string | undefined;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  dateOfBirth?: number | undefined;
  @sql.SqlColumn(sql.ColumnType.BOOL)
  forum: boolean;

  constructor(chatId: number, chatType: ChatType, packedChat: string, isContact: boolean, isMutualContact: boolean,
    fullName: string, firstName: string, forum: boolean = false) {
    this.chatId = chatId;
    this.chatType = chatType;
    this.packedChat = packedChat;
    this.isContact = isContact;
    this.isMutualContact = isMutualContact;
    this.fullName = fullName;
    this.firstName = firstName;
    this.phone = "";
    this.username = "";
    this.lastName = "";
    this.bio = "";
    this.dateOfBirth = 0;
    this.photoThumb = "";
    this.forum = forum;
  }

  public static fromNative(native: NativeSeenChat): SeenChat {
    let seenChat =
      new SeenChat(native.chatId, native.chatType, native.packedChat, native.isContact, native.isMutualContact,
        native.fullName, native.firstName, native.forum);

    seenChat.phone = native.phone || "";
    seenChat.username = native.username || "";
    seenChat.lastName = native.lastName || "";
    seenChat.bio = native.bio || "";
    seenChat.dateOfBirth = native.dateOfBirth || 0;

    if (native.photoThumb) {
      seenChat.photoThumb = Base64.encodeToString(new Uint8Array(native.photoThumb));
    } else {
      seenChat.photoThumb = "";
    }

    return seenChat;
  }

  public toNative(): NativeSeenChat {
    let native: NativeSeenChat = {
      chatId: this.chatId,
      chatType: this.chatType,
      packedChat: this.packedChat,
      isContact: this.isContact,
      isMutualContact: this.isMutualContact,
      fullName: this.fullName,
      firstName: this.firstName,
      phone: this.phone,
      username: this.username,
      lastName: this.lastName,
      bio: this.bio,
      dateOfBirth: this.dateOfBirth,
      forum: this.forum
    };
    if (this.photoThumb !== "") {
      native.photoThumb = Array.from(Base64.decode(this.photoThumb));
    } else {
      native.photoThumb = undefined;
    }
    return native;
  }
}