API_TOKEN_PEPPERS = {1: "Qy+F=OTeGskWQ(wTMgjc+NPPlz6YwFXY=KHIIg=wpYXT&e(6u8"} ## Should be replaced
CORS_ORIGIN_ALLOW_ALL=True
EMAIL_FROM="netbox@bar.com"
EMAIL_PASSWORD=""
EMAIL_PORT=25
EMAIL_SERVER="localhost"
EMAIL_SSL_CERTFILE=""
EMAIL_SSL_KEYFILE=""
EMAIL_TIMEOUT=5
EMAIL_USERNAME="netbox"
GRANIAN_BACKPRESSURE=4
GRANIAN_WORKERS=4
GRAPHQL_ENABLED=True
MEDIA_ROOT="/opt/netbox/netbox/media"
METRICS_ENABLED=False
RELEASE_CHECK_URL="https://api.github.com/repos/netbox-community/netbox/releases"
SECRET_KEY='r(m)9nLGnz$(_q3N4z1k(EFsMCjjjzx08x9VhNVcfd%6RF#r!6DE@+V5Zk2X' ## Should be replaced
SKIP_SUPERUSER=True
WEBHOOKS_ENABLED=True
DB_WAIT_DEBUG=1
ALLOWED_HOSTS=["*"]
DEBUG=True
INTERNAL_IPS=["0.0.0.0/0"]
PLUGINS = [
    "netbox_prometheus_sd",
    "netbox_bgp",
    "netbox_qrcode",
    "netbox_ipcalculator",
    'netbox_topology_views',
    "netbox_custom_objects"
]
PLUGINS_CONFIG = { "netbox_prometheus_sd": {}, "netbox_bgp": {}, "netbox_qrcode": {}, "netbox_ipcalculator": {}, "netbox_topology_views": { 'static_image_directory': 'netbox_topology_views/img', 'allow_coordinates_saving': True, 'always_save_coordinates': False }, "netbox_custom_objects": {} }
FIELD_CHOICES = {
    "dcim.Device.status+": [
      ( 'needswupdate', 'NeedSWUpdate', 'red' ),
      ( 'testequipment', 'TestEquipment', 'indigo' )
    ],
    "ipam.IPAddress.status+": [
      ( 'quarantine', 'Quarantine', 'indigo' )
    ],
    "ipam.Prefix.status+": [
      ( 'quarantine', 'Quarantine', 'indigo' )
    ],
    "circuits.Circuit.status+": [
      ( 'draft', 'yellow' ),
      ( 'provisioning', 'yellow' ),
      ( 'deprovisioning_planned', 'red' ),
      ( 'deprovisioning', 'red' )
    ]
}
REDIS = {
    'tasks': {
        'HOST': 'redis',
        'PORT': '6379',
        'USERNAME': 'default',
        'PASSWORD': 'H733Kdjndks81', ## Should be replaced (must match REDIS_PASSWORD in redis.env)
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'HOST': 'redis',
        'PORT': '6379',
        'USERNAME': 'default',
        'PASSWORD': 'H733Kdjndks81', ## Should be replaced (must match REDIS_PASSWORD in redis.env)
        'DATABASE': 1,
        'SSL': False,
    }
}
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox',
        'USER': 'netbox',
        'PASSWORD': 'J5brHrAXFLQSif0K',
        'HOST': 'postgres',
        'PORT': '5432',
        'CONN_MAX_AGE': 300,
    }
}