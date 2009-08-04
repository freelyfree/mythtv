#ifndef MYTH_MEDIA_MONITOR_H
#define MYTH_MEDIA_MONITOR_H

#include <QList>
#include <qpointer.h>
#include <qthread.h>
#include <qstring.h>
#include <QStringList>
#include <QMutex>
#include <QEvent>

#include "mythmedia.h"

const int kMediaEventType = 30042;

/// Stores details of media handlers
struct MHData
{
    void   (*callback)(MythMediaDevice *mediadevice);
    int      MediaType;
    QString  destination;
    QString  description;
};

class MPUBLIC MediaEvent : public QEvent
{
  public:
    MediaEvent(MediaStatus oldStatus, MythMediaDevice* pDevice) 
         : QEvent((QEvent::Type)kMediaEventType)
           { m_OldStatus = oldStatus; m_Device = pDevice;}

    MediaStatus getOldStatus(void) const { return m_OldStatus; }
    MythMediaDevice* getDevice(void) { return m_Device; }

  protected:
    MediaStatus m_OldStatus;
    QPointer<MythMediaDevice> m_Device;
};

class MediaMonitor;
class MonitorThread : public QThread
{
  public:
    MonitorThread(MediaMonitor* pMon,  unsigned long interval);
    void setMonitor(MediaMonitor* pMon) { m_Monitor = pMon; }
    virtual void run(void);

  protected:
    QPointer<MediaMonitor> m_Monitor;
    unsigned long m_Interval;
};

class MPUBLIC MediaMonitor : public QObject
{
    Q_OBJECT
    friend class MonitorThread;
    friend class MonitorThreadDarwin;

  public:
    virtual void deleteLater(void);
    bool IsActive(void) const { return m_Active; }

    virtual void StartMonitoring(void);
    void StopMonitoring(void);
    void ChooseAndEjectMedia(void);

    static MediaMonitor *GetMediaMonitor(void);
    static QString GetMountPath(const QString& devPath);
    static void SetCDSpeed(const char *device, int speed);

    bool ValidateAndLock(MythMediaDevice *pMedia);
    void Unlock(MythMediaDevice *pMedia);

    // To safely dereference the pointers returned by this function
    // first validate the pointer with ValidateAndLock(), if true is returned
    // it is safe to dereference the pointer. When finished call Unlock()
    QList<MythMediaDevice*> GetRemovable(bool mounted=false);
    QList<MythMediaDevice*> GetMedias(MediaType mediatype);
    MythMediaDevice*        GetMedia(const QString &path);

    void MonitorRegisterExtensions(uint mediaType, const QString &extensions);
    void RegisterMediaHandler(const QString  &destination,
                              const QString  &description,
                              const QString  &key,
                              void          (*callback) (MythMediaDevice*),
                              int             mediaType,
                              const QString  &extensions);
    void JumpToMediaHandler(MythMediaDevice*  pMedia);

    // Plugins should use these if they need to access optical disks:
    static QString defaultCDdevice();
    static QString defaultVCDdevice();
    static QString defaultDVDdevice();
    static QString defaultCDWriter();
    static QString defaultDVDWriter();

    virtual QStringList GetCDROMBlockDevices(void) = 0;

  public slots:
    void mediaStatusChanged(MediaStatus oldStatus, MythMediaDevice* pMedia);

  protected:
    MediaMonitor(QObject *par, unsigned long interval, bool allowEject);
    virtual ~MediaMonitor() {}

    void AttemptEject(MythMediaDevice *device);
    void CheckDevices(void);
    virtual void CheckDeviceNotifications(void) {};
    virtual bool AddDevice(MythMediaDevice* pDevice) = 0;
    bool RemoveDevice(const QString &dev);
    bool shouldIgnore(const MythMediaDevice *device);
    bool eventFilter(QObject *obj, QEvent *event);

    const QString listDevices(void);

    static QString defaultDevice(const QString setting,
                                 const QString label,  
                                 const char *hardCodedDefault);
    MythMediaDevice *selectDrivePopup(const QString label, bool mounted=false);

  protected:
    QMutex                       m_DevicesLock;
    QList<MythMediaDevice*>      m_Devices;
    QList<MythMediaDevice*>      m_RemovedDevices;
    QMap<MythMediaDevice*, int>  m_UseCount;

    // List of devices/mountpoints that the user doesn't want to monitor:
    QStringList                  m_IgnoreList;

    bool                         m_Active;      ///< Was MonitorThread started?
    bool                         m_SendEvent;   ///< Send MediaEvent to plugins?
    bool                         m_StartThread; ///< Should we actually monitor?

    MonitorThread                *m_Thread;
    unsigned long                m_MonitorPollingInterval;
    bool                         m_AllowEject;

    QMap<QString, MHData>        m_handlerMap;  ///< Registered Media Handlers

    static MediaMonitor         *c_monitor;
};

#define REG_MEDIA_HANDLER(a, b, c, d, e, f) \
        MediaMonitor::GetMediaMonitor()->RegisterMediaHandler(a, b, c, d, e, f)

#endif // MYTH_MEDIA_MONITOR_H
