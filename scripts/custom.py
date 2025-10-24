from jmcomic import JmDownloader, JmAlbumDetail

class CustomJmDownloader(JmDownloader):
    def before_album(self, album: JmAlbumDetail):
        id = album.id # 将此 album 的信息更新到所有匹配的下载任务中
        return super().before_album(album)